# Kit 8 design notes

This file holds the detailed design of the Marketplace Settlement Agent: the ledger and its
identities, how money in flight is resolved, request ids, the reserve formula, the approval
matrix, the operator tool for money stuck past the deduplication window, operator commands and
the readings taken on each guide ambiguity. [README.md](README.md) is the overview; read it
first.

The kit is pure Python and imports only `awx_common` (the deduplication margin is
`awx_common.ops.DEDUP_RESEND_MARGIN`). Runner rules 0 to 11 and kit rules M1 to M4 are defined
in the `awx_common.workflow` module docstring and in [docs/workflow-rules.md](../../docs/workflow-rules.md).

## Modules

| Module | Holds |
|---|---|
| `ledger.py` | `SellerLedger`, `settle_cycle`, the seller, cycle and platform identities, `platform_residual` |
| `reserve.py` | `ReservePolicy`: rate formula, bounds, materiality, dual-approval threshold |
| `evidence.py` | strict carrier-event parse, exposure from the seller's own order book, fenced model view |
| `operations.py` | payout and recovery builders (`awx_common.bind_platform_operation`), statuses, reconcile, `resolve` |
| `store.py` | kit state on `PolicyContext.store`, a KitStore scoped to kit 8 and ONE seller |
| `common.py` | input validation, tiers, reserve targets with carried-forward evidence holds, the account check |
| `settle.py`, `recalculate.py`, `recover.py`, `reconcile.py`, `resolve.py` | one spec each; `workflow.py` assembles `SPECS` |
| `watch.py` | watches on `not_landed` decisions and the counterparty scan |
| `tools.py`, `reads.py` | `TOOLS` and `READS` (`ctx.read` and `ctx.store` only) |
| `commands.py` | `OPERATOR_COMMANDS` |

## The ledger

The guide's report names a reserve ledger but gives no schema; this is the kit's. All fields
are cumulative, non-negative `Money` in the seller's currency: `entitled`, `refunds_paid`,
`reserve_held`, `paid_out`, `payout_pending`, `payable` (a failed payout still owed),
`recovered`, `receivable`. Every posting checks these identities and refuses to post if one
fails:

- Seller: `paid_out + payout_pending + payable + refunds_paid + reserve_held = entitled +
  recovered + receivable`.
- Cycle (the guide's "payouts, refunds and released reserve equal what the seller was owed"):
  `entitlement + payable_before + released + drawn + shortfall = payout + refunds + retained +
  receivable_offset`.
- Platform: platform cash minus its own cash equals `sum(reserve_held + payable - receivable)`.
  With nothing owed, that is the guide's "remaining balance equals unreleased reserves".
  `payout_pending` is not platform cash: Airwallex debits the platform when the transfer is
  created (FAKE-FIDELITY.md), so a posted payout that never reached Airwallex shows as a
  residual of exactly its amount until it is resolved. `tests/test_oracle.py` checks this residual.

`settle_cycle` order: refunds are paid from the cycle's cash, then from reserve (`drawn`); what
is left over is a `shortfall` and becomes a receivable. Then the reserve moves to target,
funded only from the cycle's remaining cash (any gap is `unmet_reserve`, reported and never
charged). Then an outstanding receivable may be netted off the payout (with approval).

The seller and cycle identities are checked by hypothesis over generated histories, and
`settle_cycle` is also checked against an independent closed form, because the identity alone
is tautological.

## State and tenancy

All kit state lives in `ctx.store`, a KitStore already scoped to kit 8 and one seller. Keys,
all tenant-local:

| Key | Holds |
|---|---|
| `book` | `{"book_id"}`: random id minted once with the book (rule M2) |
| `profile` | `SellerProfile` |
| `account` | the connected account `seed_seller` checked and bound to this seller |
| `cycle/<cycle_id>` | `CycleData` |
| `orders` | the seller's order book |
| `evidence/<event_id>` | raw carrier event |
| `override/<event_id>` | an approved evidence-driven reserve hold, carried forward |
| `ledger` | `{"ledger": SellerLedger, "postings": {key: payload}, "recoveries": {attempt: RecoveryAttempt}}` |
| `sent/<kind>/<ref>` | when money first left and the proposal_id of the run that sent it (M4) |
| `attention/<kind>/<ref>` | a SUSPENDED payout or charge awaiting a person |
| `resolution/<kind>/<ref>` | a person's decision on money past the window, and its watch |

The ledger, its posting keys and the recovery-charge claims live in ONE item, written with
optimistic concurrency, so a posting and its idempotency marker land together or not at all.
Values round-trip through `awx_common.transport`. Another seller's ledger or evidence is not
reachable at all; evidence is indexed per seller, and a forged event that names another seller
is also refused by its `seller` field.

`ledger.platform_residual` computes the cross-seller platform identity. No workflow action runs
it (that would need a platform-scope spec with `read_platform`); the tests check it after every
scenario.

With no `ctx.store`, every spec is BLOCKED and every store-backed read raises.

## Account binding

`seed_seller` binds one connected account to one seller, and every money action and every
seller read checks that binding (`common.account_problem`). Unless the run tenant's connected
account (from the tenants table) is the account seeded for that seller, the kit answers only
"the tenants table and the seeded account disagree": no figures, no account id and no Airwallex
call. The row's account id and the seeded account id go to the operator log.

## Resolving a payout or a charge (rule 2 and M1)

The build posts the cycle (`payout_pending`), records when the money first left and which run
sent it (`sent/payout/<cycle>`, owner = that run's proposal_id, rule M4), and returns the payout
op. The postcondition always runs and reads the provider back (platform reads). Kit rule M1:
only a definitive outcome moves money.

| What the read shows | Payout | Recovery charge |
|---|---|---|
| SETTLED | paid_out | receivable reduced |
| FAILED at create (not a duplicate), or the provider's FAILED state (CANCELLED only with its `failure` block) | payable, re-paid inside a later cycle under that cycle's request_id | attempt FAILED / INSUFFICIENT_FUNDS, receivable kept, a new attempt needs a new proposal |
| SUSPENDED | in flight; "operator action required" | in flight (SUSPENDED) |
| OUTCOME_UNKNOWN, an empty `?request_id=` read-back, no result, NEW, PENDING | in flight on its ORIGINAL request_id | in flight (PENDING) |

Money in flight is its own ledger state: a payout stays in `payout_pending` (never payable), and
a later cycle's payout excludes it, so it is never paid again under a new request_id. A charge
stays PENDING: the receivable is not cleared, and `settle_cycle` will not net the part a charge
in flight may be collecting (`receivable_in_flight`), so it is never collected twice either.

Getting money out of flight:

- `propose_reconcile_seller_money` reads back every in-flight payout and charge of the seller
  and records any definitive outcome. It never sends a write.
- Re-proposing the same cycle (or `propose_recover_shortfall` while a charge is in flight) is a
  resend: the build reads back by request_id first and, only if Airwallex has no record,
  re-emits the identical Operation (same request_id, body and original intent_hash). `connected_account_transfers/create` and
  `charges/create` deduplicate on request_id (FAKE-FIDELITY.md, request_id idempotency), so a late
  arrival of the original is refused as a duplicate. On a resend a refusal at create is NOT
  treated as definitive, because the first send may still land after it.
- Past the deduplication window (`DEDUP_WINDOW` = `awx_common.ops.DEDUP_RESEND_MARGIN`, 6
  days, inside the window of about 7 days the guide gives) nothing is re-sent: the resend is BLOCKED as "needs a
  person" and reconcile reports NEEDS_PERSON. The kit stalls for 6 days when a payout was
  posted but never sent (the run died before the send) and its resend is then genuinely
  refused at create. The kit cannot tell that refusal from one that races a first copy still in
  transit, so it keeps the money in flight until the window passes and a person decides. The
  stall is slow but never pays twice.
- A FAILED create is looked up by request_id before it is treated as final, because the
  executor may already have resent after an unknown first reply; an object it finds wins. An
  unreadable lookup is not definitive.
- A retryable FAILED (429, 5xx before acceptance) as the last reply is never definitive, in
  any mode: the kit reads back, else keeps the money in flight.
- A SUSPENDED payout or charge is persisted as an `attention/<kind>/<ref>` marker, listed in
  `read_seller_ledger` (`needs_person`) and in every later settlement's reasons, and cleared
  when a definitive outcome is recorded.
- A final unknown code (`request_id_reused`, `request_id_bound_to_another_body`,
  `multiple_objects`, `first_attempt_unknown`, `past_dedupe_margin`) or an undetermined
  read-back flags `needs_person` at once, in reconcile and in the settle and recover
  postconditions; nothing is re-sent. A transient read error does not flag a person.
- Inside one run, the spine's executor reports any refusal after an uncertain send as
  OUTCOME_UNKNOWN with `body["last_error"]`, and two objects under one request_id as
  OUTCOME_UNKNOWN `multiple_objects`. The kit keeps the refused resend and the `multiple_objects` reply in
  flight. The kit's own defence (a
  retryable FAILED is never definitive) stays as defence in depth.

`StepSpec.reconcile` is `operations.find_by_request_id`, which checks amount, currency and party
(`operations.matches`) and raises `ReconcileUndetermined` on a mismatch. An object under our
request_id that belongs to another payment never lends its status to ours.

`payout_found_after_failed` and `recovery_found_after_failed` stay as defence in depth: no path
in the kit posts a failure without a definitive outcome, so these corrections run only for a
watched `not_landed` decision (below). The oracle property test asserts the payout correction
never appears otherwise.

### Netting versus recovery

Recovery claims live in the same versioned ledger item as the postings. `recover_shortfall`
writes its PENDING claim conditional on the ledger version it checked the receivable at, and a
settlement posts conditional on the version it computed its netting from (which already
excludes receivable claimed by an in-flight charge). Whichever writes second gets a version
conflict and fails closed with nothing sent, so the same receivable is never both netted off a
payout and collected by a charge. A crash between the claim and the send leaves the claim
PENDING: settlements keep excluding it, and the next `recover_shortfall` re-emits the identical
charge. `tests/test_race_dynamodb.py` runs this on `DynamoDbKitStore` (moto).

### Recovery charges

Recovery reads the seller wallet first (customer scope), charges only if it covers the whole
receivable, and re-reads before executing. The live balance is not in `computed` (the proposal
hash covers `computed`, so any wallet movement would make the approval stale); the build skips
the charge if the wallet fell short. On `InsufficientFunds` the kit records the attempt and
stops. Another attempt is a new proposal with a new attempt number, request id and approval,
and only after a definitive outcome. The kit never charges part of a receivable.

## Request ids and the book id (rules M2 and M4)

Request ids derive from business keys that name the object and the book, never from a
proposal or intent hash (`derive_request_id`):

- `payout:<book>:<seller>:<cycle>`
- `recovery:<book>:<seller>:<attempt>`

The `seed_seller` operator command mints a random book id (`uuid4`) once, with the seller's
first state, and `create_book` refuses a second mint; a reseed with `replace: true` keeps it. A
fresh book seeded like an earlier one gets disjoint request_ids, so a second run on the shared
sandbox (which deduplicates for about 7 days) can never adopt the first run's payouts or
charges. A seller with no book id is BLOCKED. After a definitive failure the next recovery
attempt has its own `<attempt>` key segment, so deduplication can never wedge a retry.

Holds and claims (evidence holds, sent markers, recovery attempts, resolution records) record
the proposal_id of the run that took them (M4). The kit has no `on_abandon`, so an abandoned
proposal releases nothing.

## Reserve formula

`rate = min(max(trailing_refund_rate x 2, tenure floor), 30%)`, where the floor is 20% under 90
days, 10% under 365 days and 5% after that. Target = rate x trailing volume, rounded up. The
falling floor gives the newest seller the highest reserve, as the guide asks; with equal refund
history a newer seller never has a lower rate (property test). Carrier evidence sets an override
target `max(rate target, exposure)`, where exposure is the open value of the event's undelivered
orders found in that seller's own order book. The hold carries into every later cycle, capped at
the approved target and limited to the open value of its orders that are still undelivered, so
it shrinks by itself as the carrier delivers or the refunds are paid.

Recalculating and settling one seller leaves the other sellers' stores byte-identical
(property test); the override is keyed per event in the seller's own store.

## Model and code

The model reads ledgers, balances and carrier events, summarises the narrative and explains.
Tool inputs are only `cycle_id`, `carrier_event_id`, (for the balance read) `currency`, and
for the resolve tool `kind`, `ref`, `resolution` and `evidence_ref`. Code computes every reserve, payout, shortfall and charge amount,
picks every account (from the server-resolved tenant) and every request id. The carrier
narrative reaches the model fenced as untrusted text; code never parses it, and the fence
markers are stripped from it so it cannot close its own fence (prompt-injection test).

## Approval matrix

| Action | AUTO | SINGLE_APPROVER | DUAL_APPROVER | BLOCKED |
|---|---|---|---|---|
| `settle_seller_cycle` | reserve unchanged, released, or raised non-materially; a resend of an in-flight payout | material reserve increase; receivable netted off the payout; a new receivable | payout, reserve increase or netting >= 10,000 | no connected account; account binding mismatch; cycle already settled; ledger out of balance; missing data; currency mismatch; a watch holds it |
| `recalculate_seller_reserve` | exposure within the current target (no change), or a non-material increase | material increase | increase >= 10,000 | evidence names another seller; cycle already settled; malformed evidence |
| `recover_shortfall` | wallet short: no charge, receivable kept; a resend of an in-flight charge | every new recovery charge | charge >= 10,000 | nothing owed; in flight past the dedup window; a watch holds it |
| `reconcile_seller_money` | always (sends nothing) | - | - | nothing in flight; no book id |
| `resolve_in_flight_money` | - | - | always | not in flight; still inside the dedup window; never sent; Airwallex shows the object |

Material increase: more than 500 or more than 25% of the reserve before, or any increase from
zero. Thresholds are major units of the seller's currency and live in `ReservePolicy`. A
material increase is approved twice when it comes from evidence (once when the evidence is
accepted, once when the cash is withheld), which is the cautious reading.

Approver scope (`KitWorkflowSpec.approver`): `settle_seller_cycle`,
`recalculate_seller_reserve` and `resolve_in_flight_money` are approved by the kit 8 operator
(`ApproverScope.KIT_OPERATOR`), because they decide what a seller is paid or owed;
`recover_shortfall` and `reconcile_seller_money` by the seller's own approver
(`ApproverScope.OWN_TENANT`). The approver must always differ from the proposer
(`awx_common.approvals`).

Resends (re-proposing a settled cycle whose payout is in flight, or recover while a charge is
in flight) are AUTO at policy level, because they re-emit an already-approved operation with
the same request_id, but the spec floor stays SINGLE, so a resend the model starts waits for a
human; scheduled runs (origin SCHEDULE) keep the policy tier.

`reconcile_seller_money` sets `agent_min_tier=RiskTier.AUTO`. Justification: its step emits no
Operation (a contract test asserts its build returns nothing), it moves no money, its computed
is the list of in-flight items taken from the seller's own store, and every outcome it records
comes from a provider read checked against the operation (amount, currency, party). Under M1 it
may only record definitive outcomes. An injected instruction can at most make the agent record
what Airwallex already shows.

## The operator tool: `resolve_in_flight_money`

Past the 6-day window the kit sends nothing, because a resend could create a second object once
Airwallex forgets the request_id (a test shows the fake doing exactly that). Instead it flags
the item (`read_seller_ledger.needs_person`). A person asks Airwallex what happened and
proposes:

- `landed`: the payout or charge is posted as SETTLED.
- `not_landed`: a payout becomes payable and is re-paid inside a later cycle under that cycle's
  request_id. A charge attempt becomes FAILED and the receivable stays owed, so a new attempt
  can follow on its own key segment.

Every proposal carries an `evidence_ref`, for example a support case id.

Guards:

- The decision always needs two approvers, and the kit 8 operator approves.
- The policy requires the item to be in flight and past the window, and a read-back must still
  find nothing. If Airwallex shows the object, `reconcile_seller_money` records it instead.
- Only a clean, empty read-back can be resolved. Two objects under the request_id, a mismatched
  object or an unreadable reply BLOCK the decision and name the request_id. The engine retries
  a transient read 3 times; more faults than that BLOCK.
- The build repeats every check, so a decision that has gone stale fails closed.
- It sends nothing. The decision is stored with its owner proposal_id (M4).
- The approver's reason says why the item is stuck (the attention marker, for example "in
  flight past the deduplication window" or "SUSPENDED at Airwallex").
- `evidence_ref` is checked for format only. Nothing verifies it against Airwallex.

### Watches on `not_landed`

A `not_landed` decision moves money on Airwallex's word rather than Airwallex's own failure
state. If the original request lands after all, the seller would be paid or charged twice, so
every `not_landed` decision stays on a watch list (`watch.py`) with no time limit:

- The watch is the decision record itself (`resolution/<kind>/<ref>`), written BEFORE the
  ledger move. A crash between the record write and the ledger move leaves the record with the money still in flight
  (`decision_applied == "missing"`): nothing is payable, the in-flight read-back still runs,
  and the same proposal's retry finishes the move (runner rule 0). No crash can lose a watch.
- `reconcile_seller_money` reads each watch back, read-only, and maps what it finds through the
  kit's own `read_outcome`: SETTLED is posted as a late landing (`payout_found_after_failed` /
  `recovery_found_after_failed`), flagged `needs_person` naming the owner proposal, and closed
  as `found`. Airwallex's own failure state (FAILED, or CANCELLED with a failure block) closes
  it as `confirmed_failed`. PENDING, SUSPENDED, CANCELLED without failure, two objects or a
  mismatch keep it open, post nothing and flag a person. A rebuilt request_id that differs from
  the watched one reads as undetermined.
- Prevention: until a reconcile at least 24 h (`CONFIRM_AFTER`) after the decision reads the
  watch empty AND a counterparty scan is clean (`confirmed_at`), `settle_seller_cycle` (payout
  watches) and `recover_shortfall` (charge watches) BLOCK, and settlement never nets a watched
  charge's receivable. Each of them also BLOCKs at any time while its watched original shows at
  Airwallex, and lists every open watch in its reasons. Confirmed watches stay open and are still read on
  every reconcile.
- A charge watch that shows at Airwallex (anything but empty or its own failure) also holds
  `settle_seller_cycle`, confirmed or not, because netting would take a receivable the charge
  may already have collected.
- The decision's own run never re-pays or re-charges: it sends nothing.

### Counterparty scan

At decision time, during the first 24 h, and at the confirming read, the kit pages
`GET /api/v1/connected_account_transfers` or `/api/v1/charges` (`page_num`, `page_size` =
`SCAN_PAGE_SIZE`, 100) and matches `destination` / `source` client-side, because neither the
fake nor the sandbox (as far as the spine's probes go) filters those lists by counterparty. Any
non-FAILED, non-CANCELLED movement of the watched amount since the first send (less 5 minutes)
that is not one of this book's own request_ids BLOCKS. Movements to or from other sellers on the
list are ignored. The scan shares the list's visibility (an object invisible to `?request_id=`
is invisible here too), which is why the second read waits a day.

The scan fails closed: a movement with a missing or unreadable `created_at` counts as inside
the window, and a reply with no readable `has_more` is read as the last page only if it
returned fewer than `SCAN_PAGE_SIZE` items. It BLOCKs ("did not finish") after
`SCAN_MAX_PAGES` (50) pages. In the live sandbox `has_more` is a bool on the transfer list and on the
charge list,
`created_at` looks like `2026-10-06T23:42:38+0000` (which the kit parses), items carry
`short_reference_id` and `updated_at`, and lists are newest first (the fake is oldest first;
the kit pages every item and filters, so order does not matter).

Cost: watches never expire. Every settle and recover makes one `?request_id=` read per open
watch, and one full counterparty scan per unconfirmed watch younger than 24 h; every reconcile
reads every open watch. This cost suits proof-of-concept scale.

### Adopting a stranded decision

A decision whose run died after its record and before its ledger move (the money still in
flight) can be adopted by a new DUAL-approved proposal for the SAME resolution, but only once
`ADOPTION_WAIT` has passed, so the original run can no longer finish it. The policy reason
names the old proposal. The build rewrites `owner_proposal_id` with a versioned put, keeps
`previous_owner_proposal_id`, then applies the move once, so a late-resuming original and the
adopter cannot both win. Inside the wait a re-proposal is BLOCKED at policy, so approvers never
sign a proposal that would then fail at build.

The kit cannot see whether the old execution is terminal, so it waits whatever that state is. The wait
(`common.ADOPTION_WAIT`, the 4 h approval time to live) runs from the latest owner change,
`max(decided_at, adopted_at)`, so a live adopter cannot be pre-empted by a second adopter. It
rests on one assumption: after `decided_at` (written inside the Build task) the original run
has only that task's retries left, `BUILD_RETRY_BUDGET`, and the resolve spec has no later step.
The synthesised Build state carries CDK's LambdaInvoke default retry: 6 retries (7 attempts),
interval 2 s, backoff rate 2, service errors only, never `States.ALL`; each attempt is a 60 s
step Lambda. 7 x 60 s + (2+4+8+16+32+64) s = 546 s, about 9 minutes. `tests/test_synth_k8.py`
reads every `Build*` state's Retry list and the step Lambda timeout off the synthesised state
machines and checks them against these constants. `PolicyContext.run_status` could replace the
budget; the kit does not use it.

`not_landed` is only as good as Airwallex's answer. If an object Airwallex said never existed
appears after the payable was already re-paid, the money has moved twice. The watch finds it
and books the overpayment as a receivable for a person, but nothing undoes it automatically.
This is why the tool needs two approvers and evidence.

## Operator commands

A deployed kit runs no setup code, so every piece of seller state reaches it through
`awx_k8_settlement.OPERATOR_COMMANDS`, which `awx-deploy` runs (`apply` for the steps in
`deploy/k8.yaml`, `run` for one command) through `awx_common.operator_commands.run_operator_command` against the deployed stores. The
commands are not tools, workflow actions or declared `FUNCTIONS`; the agent cannot reach them. They move no
money: the buyer money behind a cycle is a separate sandbox deposit to the platform wallet,
done under `flock /tmp/awx-sandbox.lock`. Amounts are always `{"amount": "<decimal string>",
"currency": "USD"}`; a JSON number is refused.

Order, per seller:

1. `seed_seller` (once). Args: `seller` (the tenant id: `alice`, `bob`, `cara`),
   `connected_account_id`, `currency` (`USD`), `tenure_days` (int), `trailing_refund_rate`
   (decimal string in [0, 1]), optional `replace`. Before writing it reads
   `GET /api/v1/accounts/{connected_account_id}` at platform scope and refuses unless the
   account exists, reads `status` ACTIVE, and its `nickname` (or business name) starts with
   `poc-i8-`; it refuses offline (`read` is None) and refuses placeholder ids. It mints the book
   id. A second run is refused unless `replace: true`, which keeps the book id and cannot change
   the currency.
   - One connected account per seller: the kit's platform store holds one item,
     `seller-accounts` (`{"accounts": {acct: seller}}`), changed by one versioned
     read-modify-write before any seller record. Seeding a second seller on a bound account is
     refused; of two concurrent seeds one wins and the other is refused having written nothing.
   - A replace never moves a seller to another account; to move one, seed a new seller.
   - The tenants row for each seller must name the same `acct_...` (see "Account binding").
   - Repairing a half-done seed (there is no unbind command): if a seed died after the index
     write and before the `profile` write, re-run `seed_seller` for the same seller and
     account, which completes it. If it died after the `profile` write but before the `account`
     write, a plain re-run answers "already seeded": re-run it with `replace: true` (same
     seller, same account), which keeps the book id and writes the missing `account` record. To
     free the account instead, rewrite the `seller-accounts` item without that entry by hand,
     and only when that seller's store holds no records (no `book`, `profile` or `account`).
   - The ids to pass are the `poc-i8-seller-alice`, `poc-i8-seller-bob` and
     `poc-i8-seller-cara` connected accounts (`acct_...`), created once and then found by label
     (`K8_ACCT_ALICE`, `K8_ACCT_BOB`, `K8_ACCT_CARA` in `deploy/k8.yaml`).
2. `load_orders` (feed). Args: `seller`, `as_of` (ISO UTC), `orders`: a list of `{order_id,
   amount, refunded, delivered}`. Keyed per `order_id` and `as_of` (kept on each row of the one
   `orders` item): the same row again is a no-op, a newer `as_of` updates the row, the same
   `as_of` with other content, an older one, or an order_id twice in one batch is refused.
3. `load_carrier_events` (feed). Args: `seller`, `events`: carrier events as the carrier sends
   them (`event_id`, `seller`, `carrier`, `event_type`, `order_ids`, `observed_at`,
   `narrative`). Keyed per `event_id`; an event naming another seller is refused. The text stays
   untrusted data.
4. `load_cycle` (feed), each settlement cycle. Args: `seller`, `cycle_id`, `entitlement`,
   `refunds`, `volume_base`. Keyed per `cycle_id`. Then deposit the cycle's net buyer money
   (entitlement minus refunds, summed over sellers) into the platform wallet, as a separate step.

Every feed is idempotent: the same input again is a no-op, different content under a loaded key
is refused. A refused command writes nothing: each batch is validated whole before any write,
and every refusal is an `OperatorCommandError`. Every report is a JSON object of what was
written (seller, book id, ids, counts); none holds a secret.

## Guide ambiguities and the reading taken

- `x-on-behalf-of` on platform calls: never sent. On `connected_account_transfers/create` the
  header makes the named account the payer (FAKE-FIDELITY.md, platform money movement:
  `x-on-behalf-of` makes the named account the payer). Ops are bound with
  `awx_common.bind_platform_operation` (field from `PLATFORM_BODY_FIELDS`: `destination` for
  the payout, `source` for the charge) and pass `check_tenant_scope` before they leave the kit.
- `reason`: the payout and the recovery charge use `goods_purchased`, which was verified live on
  `connected_account_transfers/create` and `charges/create`
  (FAKE-FIDELITY.md, platform money movement: `reason`) and which describes marketplace proceeds. `settlement` and
  `commission` pass the first validator and then fail.
- Statuses (FAKE-FIDELITY.md, platform money movement: terminal statuses; API reference): NEW, PENDING, SETTLED, SUSPENDED, FAILED.
  SETTLED is success, FAILED is failure, SUSPENDED needs a person. CANCELLED is not documented
  for these objects; if one reads CANCELLED, the kit treats it as a failure.
- Partial recovery: the guide says the charge fails if the balance is short. The kit never
  charges part of a receivable; a short wallet means no charge.
- Refunds: the flow lists no buyer-refund endpoint, so refunds are cycle inputs that the
  platform is assumed to have paid from its own wallet. Reconciliation counts them as cash out.
- Materiality, the reserve formula and its bounds are not in the guide; they are AWS-authored
  policy constants.
- The platform is the authorised marketplace (merchant of record) that collects buyer payments,
  owes sellers their net earnings and is liable for refunds after payout. Sellers have agreed to
  a reserve and to recovery charges against their wallet.
- One currency per seller; a cycle in another currency is BLOCKED.
- Balance amounts arrive as Decimal or string in `{"items": [...]}`; a float is refused. A
  missing currency row means zero.

## Sandbox limits

- Platform money movements cannot be made to end SUSPENDED or FAILED in the sandbox
  (FAKE-FIDELITY.md, platform money movement: terminal statuses). Those paths are tested only through the `KitPort` wrapper in
  `tests/conftest.py`.
- The "seller has no funds, so no charge" step of the acceptance test cannot happen live after
  a payout: the seller's sandbox wallet holds that payout, so the kit proposes the charge instead.
  The no-funds branch is covered on the fake only.
- Sandbox object labels are `poc-i8-` (`operations.LABEL`); code finds sellers' connected
  accounts by it.
