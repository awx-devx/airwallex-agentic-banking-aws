# Kit 2 design notes

This file holds the detailed design of kit 2: the plan and control rules, the approval design,
the limit-boundary test, request ids and duplicate guards, unknown outcomes, and the reading
taken on each guide ambiguity. [README.md](README.md) is the overview. Runner rules 0 to 11
and kit rules M1 to M4 are defined in the `awx_common.workflow` module docstring (see also
[docs/workflow-rules.md](../../docs/workflow-rules.md)).

## Package layout

| Module | Contents |
|---|---|
| `workflow.py` | `SPECS` (`issue_card`, `update_card_controls`, `freeze_card`, `unfreeze_card`, `close_card`, `run_limit_test`), `APPROVAL_MATRIX`, policies, step builders, finality and postconditions |
| `tools.py` | `TOOLS`: `read_purchase_plan`, `read_card_controls`, `read_card_transactions` and `propose_<action>` for every spec |
| `reads.py` | `READS`, the handlers for the read tools (pure: `ctx.read` only) |
| `reconcile.py` | the `StepSpec.reconcile` hooks for cardholder create, card create, card updates and every probe |
| `terms.py` | terms parsing, the cash curve, plan choice, reconsideration and `controls_for` |
| `controls.py` | `CardControls`, the provider payload and the "reduces or equals" order |
| `harness.py` | limit-test probe planning (`plan_probes`) and judging (`judge`) |
| `provider.py` | provider state maps and read parsing |
| `ops.py` | operation builders, business keys and labels |
| `policy_config.py` | server-side policy values |

## Plan and controls

### Plan rule

The cheapest plan that keeps cash at or above the reserve floor on every day of a 13-week
(91-day) horizon. A tie goes to monthly. If no plan keeps the floor, the proposal is
BLOCKED. The annual price comes from a stated discount with `annual_price_from_discount`,
rounded half-up to the minor unit, when the terms give no annual price.

### Control set

`terms.controls_for` returns the least authority that pays the chosen plan until the next
reconsideration:

- Monthly: PER_TRANSACTION = MONTHLY = one month's price; ALL_TIME = the charges dated up to
  the reconsideration date. Any later charge needs a fresh, approved raise.
- Annual: one single-use charge; every limit equals the annual price.
- Always exactly one allowed currency (the card's) with every limit in it, so no limit check
  needs a foreign exchange (FX) rate.
- Merchant categories come from the purchase category through the policy's merchant category
  code (MCC) table (`SAAS_SUBSCRIPTION`: 5734, 5817, 7372).

`CardControls.__post_init__` refuses a control set with no cumulative limit (MONTHLY or
ALL_TIME), and `controls_for` always sets MONTHLY and ALL_TIME. For `update_card_controls`, the card's lifetime
spend so far is added to ALL_TIME.

### Reconsideration

`terms.reconsideration` returns the earlier of start + 90 days and 14 days before the
cancellation deadline (renewal date minus notice), never before the day after the start date.
It is returned with an EventBridge Scheduler one-time expression `at(YYYY-MM-DDT00:00:00)` in
UTC. The deployment creates no schedule; a production build would schedule a re-run of
`read_purchase_plan` that proposes an update if needed.

### Policy values

The reserve floor (25,000 in each supported currency), the dual-approval threshold (10,000),
the ALL_TIME cap (50,000), the 13-week horizon, the 90-day review and the 14-day lead are
AWS-authored proof-of-concept (PoC) assumptions in `policy_config.py`. The policy is versioned
(`k2-policy-2026-10-06.1`) so an approval binds to the policy it was evaluated under.

## Approval design

| Action | Tier | Why |
|---|---|---|
| `issue_card` | SINGLE; DUAL when ALL_TIME > 10,000; BLOCKED > 50,000 or when no plan keeps the floor | creates persistent spending authority (cardholder and card) |
| `update_card_controls` | AUTO if it only reduces authority in every dimension, also when the model starts it; else SINGLE or DUAL | a raised limit is new persistent authority |
| `freeze_card` | AUTO, also when the model starts it | only revokes authority; the agent can revoke itself mid-run |
| `unfreeze_card` | SINGLE | restores authority |
| `close_card` | SINGLE | irreversible; freeze is the autonomous revocation |
| `run_limit_test` | SINGLE, with the debit in `computed` and the reasons; BLOCKED unless a fresh, funded limit-test card | the at-limit probe debits the wallet and cannot be reversed with this key |

Approvals expire after 30 minutes for `freeze_card`, 1 hour for `run_limit_test` and 4 hours
for the others.

### Freezes and reductions at AUTO
A proposal the model starts is raised to at least SINGLE (`agent_min_tier`, default SINGLE,
runner rule 11). `freeze_card` and `update_card_controls` set `agent_min_tier=RiskTier.AUTO`;
every other action keeps the SINGLE floor, including `unfreeze_card`.

An action that only reduces what a card can spend is a safety action, so it must never be
delayed or lost waiting for a person. The policy returns AUTO for `update_card_controls` only
when the new controls grant no more authority than the card's current ones in every
dimension (currency, merchant categories, single use, every limit). Anything that raises
spend authority is SINGLE or DUAL and waits for a person, and `unfreeze_card` stays SINGLE. So
the worst an injected instruction can do is freeze a kit 2 card or lower its limits, which
stops or reduces spending until a person undoes it. It cannot move money, raise a limit,
unfreeze, or touch a card kit 2 did not create (the `poc-k2-` label gate). The guide asks for
this: the agent revoking its own authority mid-run must not wait on a person. Tested by
`test_injected_document_cannot_unfreeze_or_raise_without_approval`.

### Approval binding and stale approvals

Every card action is BLOCKED on a card whose `nick_name` does not start `poc-k2-`. Approval
binds to the proposal hash: the kit, the action, the intent (terms, terms document SHA-256,
forecast flows), the computed values, the tier and the origin. Because the plan also depends on
the balance read at policy time, each step builder re-runs the evaluation and raises
`StaleApproval` if the plan or control set no longer matches what was approved.

### Freeze notices

A freeze that ends without running leaves a factual notice, never an instruction. No read
tool output tells the model what to do; a test fixture scans every `read_*` output in the
scenario suite for imperatives such as "re-propose" or "without approval".

`on_abandon` (STALE, BLOCKED, REJECTED or EXPIRED), and a build that finds its change number
claimed by another run, record "a freeze recorded at <time> (change <n>) was not applied:
<why>". The model-supplied reason is stored for the audit and never shown. The notice is tied
to the freeze's own approved change number (or, if it had none, the number it would have
taken). `read_card_controls` returns it beside the card's current status only while no other
proposal has claimed a change number at or above it. A person's unfreeze, a control change
or another freeze is a later decision on the card and hides the notice as soon as it claims
its number, whether or not its postcondition ran, so a notice can never steer the model to
undo a later human decision.

## Card transaction states

`provider.py` keeps the REST and Model Context Protocol (MCP) state maps separate
and beside the flow.
`sim_reply_state` decides a probe from the simulation reply (`process_result`,
`failure_reason`, `type`); `rest_txn_state` reads a `GET /issuing/transactions` row, keyed on
`failure_reason` and then `transaction_type: CLEARING`, never on APPROVED; `MCP_TXN_STATE` is
documentation for a team that reads over MCP and is never used.

Pitfalls the kit guards against:

| Pitfall | Guard |
|---|---|
| Mixing REST and MCP transaction states | `REST_TXN_STATE` and `MCP_TXN_STATE` kept separate in `provider.py` |
| A per-transaction limit with no cumulative limit | `CardControls.__post_init__` refuses it; `controls_for` sets MONTHLY and ALL_TIME |
| Persistent spending authority without an explicit approval | the approval rules above; tests prove nothing executes before approval and the proposer cannot approve |
| Treating insufficient funds as proof of enforcement | `harness.py` checks funding before the test; any INSUFFICIENT_FUNDS is reported as `HARNESS FAILURE` |

## The limit-boundary acceptance test

`run_limit_test` runs only on a dedicated limit-test card: one issued through `issue_card` (with
its own approval) under a `purchase_ref` starting `limit-test-`, so its `nick_name` starts
`poc-k2-limit-test-`. It never runs on a card that pays a real purchase, because the at-limit
probe uses up the authority the approver granted. The card must be ACTIVE with no spend, and the
policy is SINGLE with the debit (the at-limit amount) in `computed` and in the reasons.

Each probe is designed, and checked against the local rule model, to break exactly one rule.
Every probe is a `single_phase` simulation, so an approval comes back as an approved CLEARING
in one call. They run in this order:

1. 1.00 in a disallowed currency: expect `CURRENCY_NOT_ALLOWED`
2. 1.00 at MCC 5812: expect `MERCHANT_CATEGORY_NOT_ALLOWED`
3. the per-transaction limit + 0.01: expect `LIMIT_EXCEEDED`
4. exactly the per-transaction limit: expect APPROVED, type CLEARING (debits the wallet)
5. the lowest remaining cumulative limit + 0.01, after probe 4: expect `LIMIT_EXCEEDED`

Probes 1 to 3 run before 4 so no decline can come from a cumulative limit already used. Each
probe is decided from its own reply, which is final on 201. Its finality poll only waits until
the authorisation is listed in `GET /issuing/transactions?card_id=`. The postcondition then
joins each reply to its row on `card_transaction_data.card_transaction_id` (the simulation's
id is not the row's `transaction_id`) and checks the row's state and absolute amount (list
amounts are negative). It fails, naming the duplicate, if one probe produced two
authorisations (simulations ignore request_id, so a duplicate delivery is possible).

The sandbox checks currency, merchant category and limits before funds, so the wallet needs
only the at-limit amount. A probe whose control was missing would then reach the funds check
and come back as a clear or INSUFFICIENT_FUNDS. The run fails on a clear and on
INSUFFICIENT_FUNDS, and INSUFFICIENT_FUNDS anywhere is reported as `HARNESS FAILURE`.

Probe merchant names are at most 25 characters (`poc-k2-<probe code>-<15 hex of card id>`):
the live sandbox keeps only the first 25 characters of `merchant_info` as `merchant.name` and
puts the rest into `merchant.city`.

Named tests: `test_harness.py::test_required_acceptance_limit_boundary` and
`test_scenarios.py::test_required_acceptance_limit_boundary_end_to_end`.

## Request ids

Every request_id comes from a business key naming the object, never an intent hash, and every
key starts with kit 2's book id (rule M2).

| Business key | Used for |
|---|---|
| `<book>:purchase:<ref>` | the cardholder and the card; a second proposal for the same purchase reuses the card's request_id and is refused |
| `<book>:card:<card_id>` with step `change:<n>` | control and status changes |
| `<book>:limit-test:<card_id>` | the probes |

### Book id

The book id is `bk-<random>`, minted once by the first build that needs it with a conditional
create in the kit store. Policy only reads it (rule 0) and binds it into `computed`
(`book_id`) when it already exists, so it is in the approval and the proposal id. A fresh store
pointed at an existing card restarts change numbers at 1 but under a new book, so its
request_ids never collide with an earlier run's on the shared sandbox, which deduplicates for
about 7 days.

### Card change numbers

The n-th change kit 2 makes to a card is the business object. The policy (pure) reads the
claims under `card-change:<card_id>:` in the kit store and puts the next number in `computed`,
so the approval binds it; a change claimed in between moves the hash and the run is STALE
(rule 1). The build claims the number once, recording this run's proposal_id as the owner
(rule M4; never the intent hash, which identical proposals in different windows share). A
retry of the build finds its own claim and reuses the request_id, so the operation store
returns the first result and nothing is sent twice. A number already claimed by another
proposal fails closed. Claims are never released.

A freeze, an unfreeze and a second freeze on one day send three writes under three
request_ids, and a reduction after a raise on the same day applies. Trade-off: if an execution crashes after its
claim and is re-run from the start, rule 1 sees a new next number and the run is STALE; the
operator re-proposes and the set-to-value write is sent again under the next number, which is
harmless.

The proposal id (the execution name) covers `computed`, so freeze, unfreeze and freeze again
with the same reason inside one 30-minute window start three executions (the change number
differs), while a model retry of the same proposal reaches its existing execution.

The change numbers and the book id rely on `KitStore.put(expected_version=None)` being a
conditional create: two proposals never share a change number, and concurrent first builds
agree on one book, only if the store refuses a second create of the same key.
`test_dynamodb_kit_store.py` runs the change-number race and the first-build book race on
`DynamoDbKitStore` under moto.

## Duplicate guards and reconcile

Only card create sends `request_id`, because Airwallex rejects a reused one there (400
`invalid_argument`, no id). Cardholder create and `simulation/issuing/create` ignore it, so it
is not sent; the operation store plus each step's reconcile hook is the only duplicate guard:

- Cardholder: found by its deterministic email, `poc-k2-<book8>-<purchase_ref>@example.com`.
  The book id is in it, so a fresh book never adopts an earlier book's cardholder on the
  shared sandbox. `build_cardholder` skips the create when the cardholder already exists,
  since a reused email is a 400.
- Card: found by cardholder and `nick_name`, and its controls must equal the operation's. A
  card under the kit's `nick_name` with other controls is never adopted.
- Probe: found by card, absolute amount, currency, MCC and the probe's unique merchant name.
  More than one match, or a candidate row with no merchant name, raises
  `ReconcileUndetermined`. The executor never resends a simulation (rule 9).
- Status and control updates (`reconcile_card_update`): after a timeout or an adapter crash it
  reads the card back, and a card already showing the value counts as landed.
  `cards/update` ignores request_id, so it is never resent.

### Finality reads

Card create and cardholder create finality read the cardholder's card list and the
email-filtered cardholder list, not GET by id: during list lag a GET by id is a 404, and the
runner treats an exception in a finality read as a failure, while a list just comes back
empty. Each picks its own row with `FinalitySpec.select`, by the reply's `card_id` or
`cardholder_id`; a list of several rows is AMBIGUOUS only when the reply has no id to pick by.
Probe finality selects its row by the simulation's `card_transaction_id` on the operation's
own card only: a result with a provider id never takes another object's row.

### One purchase, one card

`issue_postcondition` lists the purchase's (book-scoped) cardholder's cards and fails, naming
the extra card ids, if it holds more than one. `issue_card` is BLOCKED up front when this book
already issued a card for the purchase_ref with other controls: "purchase <ref> already has
card <id>; change it with update_card_controls". The same terms again are a retry and settle
on the existing card.

## Unknown outcomes (rule M1)

A write with an unknown outcome is recorded under `inflight:<what>` with its request_id, owned
by the run's proposal_id (M4), and stays in flight until a later run reads a definitive outcome
for that request_id. `on_abandon` never releases an in-flight record or a claim.

- Card create deduplicates on request_id, so a re-proposal inside 6 days re-emits the same
  Operation and settles it. Past 6 days (`DEDUPE_MARGIN`, which is
  `awx_common.ops.DEDUP_RESEND_MARGIN`, before Airwallex's window of about 7 days ends)
  `issue_card` is BLOCKED and a person decides.
- A probe ignores request_id, so an unknown probe always BLOCKS further limit tests on that
  card for a person. A probe whose reply came back but whose row was never listed is recorded
  in flight too.
- In-flight `since` is the send time: each card create and probe build records
  `sent-at:<request_id>` just before the send, and a redelivered build keeps the first time.

After an uncertain card create the adapter's executor never resends it: `cards.create` has no
`?request_id=` read-back, so the executor cannot establish "did not land". The money stays in
flight on the original request_id; once the card is visible the kit's reconcile settles it,
and past 6 days a person decides. A refused or held resend (`body["refused"]` /
`body["last_error"]`) is OUTCOME_UNKNOWN like any other unknown. No kit 2 write has a
`?request_id=` read-back (cardholder create is read back by email), so `multiple_objects`
cannot reach kit 2 (pinned by `test_kit2_writes_have_no_request_id_read_back`). If two cards
ever hold one request_id, a later run with the same operation store settles on its own card
and the postcondition names the other; with a fresh operation store the create stays
OUTCOME_UNKNOWN for a person. No path makes a third card.

Rule 0: every build runs twice per proposal in tests (`build_twice`), with one send each,
clean postconditions and no stranded claim or in-flight record.

## Guide ambiguities and sandbox behaviour

- Field names were verified in the sandbox (`packages/awx_spine/FAKE-FIDELITY.md`, Issuing):
  card create (`request_id, cardholder_id, created_by, form_factor: VIRTUAL, is_personalized,
  program: {purpose: COMMERCIAL}`, `authorization_controls {allowed_transaction_count,
  allowed_currencies, allowed_merchant_categories, transaction_limits {currency, limits
  [{amount, interval}]}}` with PER_TRANSACTION, MONTHLY and ALL_TIME), the DELEGATE cardholder,
  the simulation body and its reply, the limits read with `remaining`, and the decline reasons
  `LIMIT_EXCEEDED`, `CURRENCY_NOT_ALLOWED` and `MERCHANT_CATEGORY_NOT_ALLOWED`. The guide only
  says "limits, a currency allowlist and merchant categories".
- The sandbox checks currency, merchant category and limits before funds; the guide says the
  reverse. Because each probe breaks exactly one rule, the order of the provider's checks does
  not change the test's verdict.
- The guide's `/simulation/issuing/{id}/capture` returns 400 and the lifecycle capture needs an
  admin key, so kit 2 uses `single_phase: true`.
- `merchant_info` is sent as a string and read back from the list row's `merchant.name`,
  truncated to 25 characters (seen live on 6 Oct 2026). Probe finality and reconcile depend on
  it; without a merchant name, reconcile raises `ReconcileUndetermined` rather than match on
  amount alone.
- Card create's reply carries `cardholder_id`; card-create finality reads by it.
- MONTHLY window: kit 2 sizes MONTHLY at one month's price. `FakeAirwallex` uses a rolling 30
  days. If the sandbox does too, two charges 28 days apart (31 Jan, 28 Feb) fall in one window
  and the second declines. That is the strict direction (no extra authority), visible, and
  fixed by an approved raise. Not yet confirmed against the sandbox.
- Single-use annual cards cannot run the limit test (their own count rule would confound the
  probes); the demo path is monthly.
- The platform USD wallet must hold at least the reserve floor (25,000) plus the 2.00 debit for
  the acceptance test, or the plan is BLOCKED before anything is created.

## Deployment and dependencies

Kit 2 deploys through the generic `KitStack`: one state machine per action, its tools in the
Gateway, and no kit-declared Lambda (no `functions.FUNCTIONS`, no `infra` module). Kit source
imports only `awx_common` and its own package; `awx-spine` and `awx-spine-aws` are test-only
dependencies. The live acceptance test takes `flock /tmp/awx-sandbox.lock` in-process; no
other script in the kit calls the sandbox.
