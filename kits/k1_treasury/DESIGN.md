# Kit 1 design notes

This file holds the detailed design of kit 1, the Adaptive Treasury Controller: where its state
lives, how request ids and obligation locks keep money from moving twice, finality, the release
tool and its runbook, and the reading taken on each ambiguity in the guide. `README.md` is the
overview; read it first.

Runner rules (`rule 0` to `rule 11`) and kit money rules `M1` to `M4` are defined in the
`awx_common.workflow` module docstring and in [`docs/workflow-rules.md`](../../docs/workflow-rules.md).

## Exports and state

| Name | Module | Contents |
|---|---|---|
| `SPECS` | `workflow.py` | `lock_rate`, `execute_plan`, `release_obligation` |
| `TOOLS` | `tools.py` | `read_balances`, `read_obligations`, `read_fx_rate`, `read_treasury_plan`, `propose_lock_rate`, `propose_execute_plan`, `propose_release_obligation` |
| `READS` | `reads.py` | one function per `read_*` tool |
| `OPERATOR_COMMANDS` | `commands.py` | `seed_treasury_book` |

`build_specs(config, transfer_advance=...)` builds `lock_rate`, `execute_plan` and
`release_obligation`. `SPECS` wires the sandbox
transfer driver `awx_common.sandbox.make_transfer_advance("k1")` (runner rule 10), so the kit
depends on `awx_common` only. `DEDUP_RESEND_MARGIN` comes from `awx_common.ops`.

Kit 1 runs as the platform tenant (`platform`), so its kit store is the platform store
(`k1#platform` in the ledger table when deployed). Nothing is held in process memory. Keys:

| Key | Holds |
|---|---|
| `book/current` | the treasury book (`book.py`) |
| `book/id` | the random book id (M2), minted once by `book.ensure_book_id` |
| `obligation/<id>` | the obligation lock |
| `run/<proposal_id>` | the lock attempts a run took |
| `ratelock/<CUR>` | the rate-locked quote for a bought currency |
| `quote/<quote_id>` | the quote burn: the one conversion request_id a quote may book |
| `submission/<request_id>` | the exact body sent under a request_id |
| `emitted/<oid>/a<attempt>/<request_id>` | the per-obligation index of every recorded submission |

## Workflow `lock_rate`

`propose_lock_rate` plans the named obligations and creates one quote per bought currency
(`validity: HR_1`, `PolicyConfig.lock_validity`), stored under `ratelock/<CUR>`. It moves no
money. `fx/quotes/create` ignores `request_id`, so the body carries none and a retried lock at
worst creates a spare quote; the latest lock wins.

The rate is locked first because M3 binds every sell amount to the approval. Bound to a live
indicative rate, rule 1's re-run at execution would go STALE whenever the rate moved between
approval and execution. With the lock, `execute_plan`'s policy reads the STORED quote, so
`computed` carries the exact sell, and the approval stays good until the quote expires (test
`test_rate_moving_between_approval_and_execution_does_not_go_stale`).

## Workflow `execute_plan`

1. `validate` checks shape only (obligation ids, cited contradictions). It has no context.
2. `policy` loads the book from the store. It BLOCKS a book with no id (M2), unknown ids or
   receipts, an obligation whose lock is LOCKED, SUBMITTED, PAID or NEEDS_PERSON (the reason
   names its age and whether a person must act), and a missing or unusable rate lock (missing,
   for another amount, expired or already used: lock again). It then plans at the locked
   prices, runs the whole-plan reserve check with the exact sells, and computes the band and
   tier. Every amount, floor, beneficiary, obligation version, quote id and each obligation's
   last lock attempt goes into `computed`, which the proposal hash binds. Live rates are read
   only to plan currencies that have no lock, and such a plan is BLOCKED. The last attempt in
   `computed` makes a retry after a definitive failure a new proposal id, so a new Step
   Functions execution name.
3. `convert` first takes the obligation locks, owned by the run's `proposal_id` (M4), all or
   none, and records the attempts under `run/<proposal_id>`. It reconciles each conversion
   with `GET /fx/conversions?request_id=`. For any not landed, it checks the locked quote still
   has at least 20 s left (`quote_margin`), re-reads balances (any lower than the approver saw
   fails closed) and re-runs the whole-plan check at the locked amounts with fees. Only then
   does it burn the quote, record the body and its emission index, claim the lock with a
   compare-and-swap write (`locks.claim_for_emission`) and return the conversion. Finality is
   `SETTLED`.
4. `transfer` only re-enters the locks (`locks_for(reenter_only=True)`): each must still be
   LOCKED, this run's, at the attempt the convert step took, or the run fails closed with
   nothing sent. A lock a person released mid-run is never retaken by that run. It reconciles
   each transfer with `GET /transfers?request_id=`. For any not landed, every conversion must
   have succeeded at the approved quote and sell (M3), the final reserve check must pass, and
   the body is recorded and the lock claimed before the transfer is returned.
5. The postcondition always runs (rule 2). It reads the provider and moves each lock:
   - PAID when the transfer is PAID;
   - SUBMITTED when it was accepted (or answered duplicate) but is not yet PAID;
   - NEEDS_PERSON when money moved but the obligation is unpaid, or the outcome is unknown;
   - RELEASED only after a definitive failure: a refusal this run received at create, a
     CANCELLED transfer with its failure block and nothing converted, or a build that raised
     before emitting anything, judged from the submission ledger and quote burns, never from
     `ctx.prior` (which a redelivery sees empty).

   It also checks every touched wallet against its floor.

Rule 1 re-runs the policy before the first step, so any balance movement between approval and
execution, or an expired lock, fails the run STALE with nothing sent.

### Order inside each build

The quote burn, the submission record and the per-obligation emission index are written first,
and the compare-and-swap claim on the lock second. A release cannot win between the claim and
the record, so money safety does not rest on the release's run-status gate (the tests in
`tests/test_release_races.py` run with the gate stubbed out):

- a release that reads the ledger after the record sees the emission and is BLOCKED inside the
  deduplication window;
- a release that read before it binds a lock version the claim then moves, so its
  `operator_set` raises LockMoved;
- a release whose write lands before the claim makes the claim raise ObligationLocked, so
  nothing is sent.

## Request ids and obligation locks

Request ids come from the book id plus the business object. They never come from an intent
hash or an obligation's book version (`keys.py`):

- transfer: `derive_request_id("k1", "poc-k1-<book id>-ob-<obligation>", "transfer:a<attempt>")`
- conversion: `derive_request_id("k1", "poc-k1-<book id>-conv-<CUR>-<obligation>.a<attempt>+...", "conversion")`

### Book id (M2)

The book id is random: `book.ensure_book_id` mints it with a conditional create on `book/id`
the first time a store is seeded, and keeps it across every later reseed. A fresh book seeded
from `DEMO_BOOK` therefore gets request_ids disjoint from any earlier book's, so it can never
adopt an earlier run's sandbox objects. Inside one book, a version bump keeps the request_id
(`test_version_bump_in_one_book_keeps_the_request_id`). Approvals bind the book id, because it
is in `computed`. A missing book id raises `MissingBookId`; it is never `str(None)`.

### Locks (M4)

There is one lock per obligation, `obligation/<id>`, whatever the obligation's version. Its
owner is the run's `proposal_id`, never the intent hash. `locks_for` takes the whole set or
none: if another run holds one obligation, the locks this call just took are undone before
anything is built, and their attempt is not consumed. Policy BLOCKS an obligation whose lock is
LOCKED, SUBMITTED, PAID or NEEDS_PERSON, at any version. A new version can change what is owed
only once the lock is RELEASED.

### Attempts (M1)

The attempt, and so the request_id, moves only on a retake from RELEASED. The owner's
postcondition sets RELEASED only when this run itself read a definitive failure:

- a 4xx refusal for one of the obligation's writes, with nothing else landed or unknown and the
  provider showing no transfer;
- a CANCELLED transfer carrying its `failure` block, with no conversion moved.

An empty prior or an empty provider read is never proof. A run that sent nothing because a
build raised is judged from the ledger, as above. A delivery that cannot see what an earlier
delivery sent holds the lock as NEEDS_PERSON. An accepted transfer, or a `duplicate_request_id`
that names the original, holds it as SUBMITTED, and `submitted_at` is recorded. A SUBMITTED
lock that the provider still does not show after the deduplication window
(`PolicyConfig.dedup_window`, 6 days, just inside the provider's 7-day deduplication) is
reported by `read_obligations` as needing a person, because no later run's postcondition reaches
it.

`landed_or_unknown` (`keys.py`) treats only `ValidationFailed`, `InsufficientFunds`,
`LimitExceeded` and `NotFound` failures as definitive refusals. Every OUTCOME_UNKNOWN,
including a refused resend (`body["refused"]`) and `multiple_objects`, is money in flight.

### Stricter than M1

Every OUTCOME_UNKNOWN that the provider does not confirm goes to a person. M1 would allow a
later run to re-emit the same Operation inside the window. Kit 1 re-emits within one run (rule
9 resends), but a new proposal never re-emits: the lock BLOCKS it. NEEDS_PERSON, an aged
SUBMITTED lock and a LOCKED lock left by a crashed run are cleared by a person through
`propose_release_obligation`.

## Finality

Finality polls read the `?request_id=` list and pick their own row with
`api.make_select` (`FinalitySpec.select`). With a provider_id in the create reply, only the row
with that id is ever returned; another object under the same request_id is never judged, so a
lagging list is a non-terminal poll that fails closed at the finality timeout. Only without a
provider_id does the single row carrying the request_id count, and more than one row fails closed. The
postcondition applies the same rule: a listed transfer or conversion whose id is not the one
the run was given goes to NEEDS_PERSON.

| Object | Success | Terminal | Poll |
|---|---|---|---|
| conversion | `SETTLED` | `SETTLED`, `CANCELLED` | every 2 s for 60 s |
| transfer | `PAID` | `PAID`, `CANCELLED` | every 5 s for 600 s |

Sandbox transfers go SCHEDULED, then PROCESSING, and do not move again without a simulation
call ([`FAKE-FIDELITY.md`](../../packages/awx_spine/FAKE-FIDELITY.md) `[tr1-create]`). `make_transfer_advance("k1")` takes each transfer to
SENT and then, after 30 s at SENT, to PAID. Without the driver a transfer stays PROCESSING, the
poll times out, the run fails closed and the lock records SUBMITTED.

## AUTO for agent proposals

A proposal the model starts is raised to at least SINGLE unless its spec lowers that floor.
`execute_plan` sets `agent_min_tier=RiskTier.AUTO`. It is the kit's only AUTO action that can
move money, and only within the policy's autonomous limits. The kit exists to show the same
conversion running autonomously when the forecast is supported, and needing a person when it is
contradicted; an agent floor of SINGLE would remove that. AUTO is bounded:

- The model's input is book obligation ids plus cited contradictions.
- A contradiction can only lower confidence. HIGH needs the book's bank and invoice
  corroboration flags.
- Code sets every amount, beneficiary, fee, ceiling and tier.
- AUTO is capped by the HIGH-band conversion limit (USD 10,000) and the per-currency payout
  limits.
- Policy exceptions are BLOCKED, and obligation locks stop any repeat payment.

The worst an injected document can do is select already-due book obligations within those
limits, or leave out a contradiction. Leaving one out returns the band to what the book's
corroboration already supports. Scheduled runs keep the policy tier.

`lock_rate` is AUTO for the agent too: it moves no money and binds nothing; it only fixes the
price a later, separately approved `execute_plan` may book at. A spare quote costs nothing.
`release_obligation` keeps the default SINGLE floor and its policy never returns AUTO.

## Approval matrix

`policy.approval_tier`; the strictest rule that applies wins. Limits are in the book's funding
currency (USD in the demo).

| Tier | When |
|---|---|
| BLOCKED | a requested obligation is not funded by the plan (unfundable, policy exception, unknown fee, no rate); the whole plan breaches a floor; an obligation is LOCKED, SUBMITTED, PAID or NEEDS_PERSON; no usable rate lock; no book or book id; unknown obligation or receipt ids |
| DUAL | exposure (conversion sell ceilings plus funding-currency payouts) above USD 100,000 (`single_approver_max`) |
| SINGLE | conversion total above the band's autonomous limit (HIGH USD 10,000, MEDIUM 2,500, LOW 0); a payout above its currency's autonomous limit (USD 15,000, EUR 10,000, GBP 1,000; any other currency always); the funding wallet ending within 10% of its floor |
| AUTO | otherwise |

The confidence band (`policy.confidence_band`) is LOW if any forecast is contradicted, HIGH
only if every forecast is bank-corroborated and invoice-matched, and MEDIUM otherwise
(including no forecasts). The thin-reserve rule watches the funding wallet only: a bought
currency deliberately lands on its floor, because the plan converts the minimum.

In the demo book on the fake, the supplier conversion buys EUR 6,038.55 (7,525.70 plus the
12.85 SWIFT fee, less 1,500 of headroom above the EUR floor) at the locked quote. It is AUTO
with supported evidence and SINGLE once the Acme email contradicts the forecast.

## Money-safety measures

| Risk in the guide's kit | Kit 1 handling |
|---|---|
| No operation or idempotency record for conversion and transfer | Request ids come from the obligation, through the lock attempt. An obligation lock in the store blocks a second proposal. Each leg is reconciled by request_id before any send, a submission record pins the exact body under each request_id, and the spine's operation store is the long-term guard. |
| A flat SWIFT fee left out of the plan | `PlanConfig.swift_fees`, default `SWIFT_FEES`: the largest fee in `RECORDED_SWIFT_FEES`, EUR 12.85 from the guide (the sandbox charged 6.21, and 6.22 on some transfers on 8 Oct 2026, a 7,525.70 EUR payout among them). `min_conversion` buys the need (amount plus fee) minus headroom, so the plan lands the EUR wallet on its floor and a smaller charged fee leaves it above. |
| A quote booked more than once | The sandbox returns `usage: MULTI_USE` and books a quote again, so `locks.burn_quote` lets one quote book at most one conversion request_id, across processes, and policy blocks a used rate lock. |
| Reserve checked per leg, not for the whole plan | `whole_plan_breaches` checks every touched wallet after every leg in execution order, fees included. It runs at policy, before conversions at quoted amounts, and before transfers. |

## Release tool

`propose_release_obligation` (`release.py`) is the only way a stuck lock moves. A person
approves it; the policy reads the evidence for the lock's current attempt and sets the action
and tier:

| Evidence for the lock's current attempt | Action | Tier |
|---|---|---|
| the transfer reads PAID | MARK_PAID | SINGLE |
| the transfer reads CANCELLED with its failure block | RELEASE | SINGLE |
| the transfer reads SCHEDULED, PROCESSING or SENT | none | BLOCKED |
| nothing found, and the submission ledger shows nothing emitted | RELEASE | SINGLE |
| nothing found, and this attempt's only send was refused at create | RELEASE | SINGLE |
| nothing found, emitted, past the deduplication window (M1) | RELEASE | DUAL |
| nothing found, emitted, inside the window | none | BLOCKED |
| a recorded conversion of the attempt the provider does not show, inside the window | none | BLOCKED |
| the same, past the window | RELEASE | DUAL |
| the provider object does not match, or a read fails | none | BLOCKED |
| the lock owner's `execute_plan` run is not known to be terminal | none | BLOCKED |
| past the window, but a matching transfer is listed since the lock was taken | none | BLOCKED |

RELEASE keeps the attempt, so the next run retakes the lock at attempt + 1 with new
request_ids (M1). Releasing after a conversion is safe: the converted money stays in its wallet
and the next plan buys less
(`test_converted_but_refused_transfer_is_released_by_one_person_and_repaid`). The approval binds
the lock's store version, so a lock that moved after the proposal fails the step closed
(LockMoved). The model may propose a release, but never below SINGLE, and the reason it gives
is only recorded in the lock's note.

### Owner-run gate

Nothing is released while the lock owner's `execute_plan` run may still send. The policy reads
`ctx.run_status(<lock owner>, "execute_plan")` and releases only on a member of `RUN_TERMINAL`.
`RUNNING`, None (no such run), an error, or a None callable at recheck all BLOCK. At propose
time the tool plane has no run status, so the decision is left to rule 1's recheck (a BLOCK
there moves the hash: STALE, nothing written), and the apply step reads it once more. There is
no time fallback. `execute_plan` starts no child machine, so the root run's status covers every
send. The proposal's second reason tells the approver that the owner run is checked at
execution, so the tier is not clearance.

The DUAL window is measured from the transfer's first submission record (`recorded_at`). That
is sound because kit 1 re-emits a transfer only inside one run (rule 9 resends, minutes apart);
a cross-run re-emission (allowed by M1) would need a `last_emitted_at` field.

### Evidence and read order

Every claim records the request_ids it covers (`claimed_rids`). The release policy reads the
lock before the submission; a build that lands between the lock read and the submission read is caught by rule 1's
recheck (`test_a_build_between_the_release_reads_is_caught_by_rule_1`), and failing that by
`operator_set`'s compare-and-swap.

The DUAL cross-check pages through `GET /transfers` from the lock's `locked_at`
(`from_created_at`, accepted live in the `%Y-%m-%dT%H:%M:%S+0000` form) and matches the
transfer reference and beneficiary. The live sandbox omits `has_more`, so without it the scan
stops only on an empty page. A page repeating an id already seen (`page_num` ignored), a
present non-bool `has_more`, a page with no items list, or more than 20 pages of 100 rows
BLOCK.

### Transfer reference

`poc-i1-<first 8 hex of the book id>-<obligation>`, cut to 35 characters. Every live object
carries the `poc-i1-` label, and the book part keeps the DUAL cross-check from matching another
book's transfers.

### Runbook: a lock whose owner run cannot be found

Such a lock stays BLOCKED by design. `run_status` answers None once Step Functions has purged
the execution (closed executions are kept for 90 days) and for a lock a `local_runner` run
took (the live acceptance test), which has no deployed execution. There is no operator command
for it. The way out is a person working in the store:

1. Gather all the evidence for the lock's current `attempt`: it is past the 6-day deduplication
   window;
   the transfer's request_id read (`GET /transfers?request_id=`) shows nothing; the paged
   `/transfers` cross-check by reference (`poc-i1-<book8>-<oid>`) and beneficiary since
   `locked_at` shows nothing that is not CANCELLED; and no recorded conversion of the attempt
   (`lock.conversion_rids`, `emitted/<oid>/a<attempt>/...`) is invisible at the provider
   (`GET /fx/conversions?request_id=`). If one is, stop: a retry could convert twice.
2. Make one conditional put on `obligation/<id>` (expected version = the version you read)
   setting `state` to RELEASED and keeping `attempt`, `owner` and every other field, exactly as
   `locks.operator_set` writes it. The next run then takes attempt + 1 and fresh request_ids.
3. Never delete the lock. A deleted lock restarts at attempt 1 and reuses the old request_ids,
   which past the provider's deduplication window can send again.
4. Record who decided, why, and the evidence (request_ids read, page count, dates) in the
   lock's `note`.

## Seeding a deployed book

A deployed kit 1 starts with an empty kit store. Until it is seeded, every read tool answers
`{"status": "NOT_CONFIGURED", "book": null, "reason": "no treasury book seeded; ..."}` and
every proposal is BLOCKED with that reason. The one shipped entry point is the operator command
`seed_treasury_book` in `awx_k1_treasury.OPERATOR_COMMANDS`, run through
`awx_common.operator_commands.run_operator_command` by `awx-deploy apply deploy/k1.yaml`, or by
the stand-alone wrapper `kits/k1_treasury/scripts/seed_deployed_book.py`.

| Command | Once | Args |
|---|---|---|
| `seed_treasury_book` | yes | `{"beneficiaries": {"<obligation_id>": "<beneficiary id>", ...}, "book_version": "<slug>", "replace": false, "scenario_start": "<YYYY-MM-DD>"}` |

- It writes the book into the platform-tenant store, mints the random book id (M2) and keeps
  only the mapped obligations, from the demo template (`ob-shipping`, `ob-supplier-eur`,
  `ob-saas-gbp`, `ob-office-usd`, `ob-contractor-gbp`).
- Every beneficiary is read through `ctx.read`: it must exist and carry a `poc-i1-` nickname.
  The command refuses when `read` is None, and refuses the template's `ben-poc-k1-*`
  placeholders. Any read failure is a refusal and writes nothing.
- It moves every due date and expected receipt by one offset (`book.moved_to`) so the
  scenario starts at `scenario_start`: an ISO 8601 (International Organization for
  Standardization) date read as midnight Coordinated Universal Time (UTC), or an ISO 8601
  date-time with a timezone. Without it the scenario starts at the seed time rounded up to the
  next whole hour, so every seeded obligation falls due within the 72 hours `read_obligations`
  reports on. The gaps between the dates stay as the template has them. The report's `due`
  lists each seeded due date. `read_obligations` marks each obligation's `due_window` against
  its own clock (`as_of`): OVERDUE, NEXT_72_HOURS or LATER.
- A second run is refused unless `replace` is true; a replace keeps the book id and moves the
  dates only. Obligation locks are keyed `obligation/<id>` whatever the book version, and PAID
  is final, so a paid obligation stays PAID after a replace and a new plan for it is BLOCKED:
  `execute_plan` pays each obligation id once per deployment. To pay again, map an obligation
  that has not been paid, as `kits/k1_treasury/examples/reseed_treasury_book.yaml` does with
  `ob-office-usd`.
- The report lists `open_locks` (every `obligation/` lock in the store that is not PAID or
  RELEASED, whichever book it came from) and `dropped_with_open_lock` (any of those this
  replace took out of the book).
- When two operators seed at once, the one whose write lands second is refused ("another seed
  landed first; re-run with replace if intended") and writes nothing.
- For `ob-supplier-eur`, pass the `poc-i1-supplier-de` EUR SWIFT beneficiary the live
  acceptance test creates (or finds by nickname). Create US LOCAL or GB LOCAL `poc-i1-`
  beneficiaries for the other obligations if the demo uses them.
- It moves no money. Funding the platform wallets (sandbox deposit simulation) is a separate
  step under `flock /tmp/awx-sandbox.lock`.

Stand-alone form (AWS credentials for the deployment account; the sandbox key is loaded by the
client and never printed):

```
uv run --all-packages python kits/k1_treasury/scripts/seed_deployed_book.py \
    --ledger-table <AwxSpine ledger table> \
    --map ob-supplier-eur=<beneficiary id> --book-version demo-1
```

Add `--replace` to replace a seeded book, and `--scenario-start <YYYY-MM-DD>` to start the
scenario on that date.

## Guide ambiguities and the reading taken

- Reserve floor: per wallet and inclusive. A wallet may end exactly on its floor. A wallet the
  plan does not touch is not judged. A wallet already below its floor is not topped up: the
  obligation escalates.
- SWIFT fees: sized at the largest fee on record, the rule kit 6 uses. For EUR that is the
  guide's flat 12.85. The sandbox charged 6.21 on 1.00 and 100.00 EUR, and 6.22 on some 10.00
  EUR payouts on 8 Oct 2026, when a plan sized at 6.21 left the wallet 0.01 EUR under its floor.
  Kit 6's live run found 6.21 too low for a 2,000 EUR payout, and on 8 Oct 2026 a deployed
  7,525.70 EUR SWIFT payout was charged 6.22 EUR. A wallet the plan lands on its
  floor therefore ends up to 6.64 EUR above it on the sandbox. SWIFT in any other currency
  escalates.
- Rate direction: sandbox-verified. `currency_pair` is fixed per pair, and `rate` is units of
  the second currency per one of the first. When the pair is BUY+SELL, sell = buy x rate. When
  it is SELL+BUY, sell = buy / rate. Anything else is refused. The plan ceiling is the
  indicative rate plus 2%, rounded up.
- Quote fields: `quote_id`, `buy_amount`, `sell_amount`, `valid_to_at`, `usage` and `validity`
  are sandbox-verified. The rate lock asks for `HR_1`; HR_4 and longer are fake-only. An
  approval that waits longer than the quote goes STALE at execution: lock again.
  (`PolicyConfig.quote_validity`, `MIN_1`, is not read by any action.)
- The lock is matched on the exact buy amount: a balance change that alters how much the plan
  must buy needs a re-lock. The policy plans with the quote's own price; the 2% plan tolerance
  on top of it only makes feasibility more conservative.
- Transfer body: `reason` is not validated on `transfers/create`, and `payment_to_supplier` is
  accepted. `source_currency` equals `transfer_currency`, so the payout comes from the
  converted wallet. The list fields reconcile uses (`transfer_currency`, `transfer_amount`,
  `beneficiary_id`) mirror the create body and are present in the live list response. The
  CANCELLED `failure` block is what the fake returns; it was not seen live, because every live
  transfer ended PAID.
- Deferral: a DEFERRABLE obligation is paid only if every non-deferrable obligation without a
  policy exception is funded, and only if paying it leaves 25% above the floor.
- A new billing period for the same obligation needs a new obligation `version` in the book.
- Refused resend: the executor reports a resend refused after the deduplication window as
  OUTCOME_UNKNOWN with the refusal in `body["refused"]`, so the lock holds
  (`test_m1_refused_resend_after_an_unknown_send_is_not_definitive`).

## Live run evidence

`evidence/live-run-1.json` is what the live acceptance test wrote on 6 Oct 2026 with
`AWX_K1_SHAPES` set: per run the outcome, tier, the conversion it booked, sanitised field
shapes of the quote, conversion and transfer rows (names and types, plus statuses and fees),
the sandbox object ids, send counts, the same-proposal and new-proposal replay results and the lock's end state, then the
paged `/transfers` read. It records that the sandbox's `GET /transfers` has no `has_more` key.
