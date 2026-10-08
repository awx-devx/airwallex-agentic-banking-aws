# Kit 6 design notes

This file holds the detailed design of kit 6 (Multi-Employer Payroll Executor): the payroll
shape, request ids, attempts, run claims, rate locks, the claim-release runbook and the
reading taken on each ambiguity in the hackathon builder guide. [README.md](README.md) is the
overview; read it first. Runner rules 0 to 11 and kit money rules M1 to M4 are defined in the
`awx_common.workflow` module docstring and in [docs/workflow-rules.md](../../docs/workflow-rules.md).

## Package surface

| Module | Export | Contents |
|---|---|---|
| `workflow.py` | `SPECS` | `run_payroll` (parent, fan-out only, `child_tenant`), `lock_rates` (parent, fan-out only) and, from `release.py`, `release_run_claim`. `PAYROLL_SPEC.child` is `EMPLOYER_SPEC` (`employer_payroll`); `LOCK_SPEC.child` is `EMPLOYER_LOCK_SPEC` (`employer_rate_lock`) |
| `tools.py` | `TOOLS` | `propose_run_payroll`, `propose_lock_rates`, `propose_release_run_claim`, `read_payroll_plan`, `read_employer_balances`, `read_payout_status` |
| `reads.py` | `READS` | `read_payroll_plan`, `read_employer_balances` and `read_payout_status`, each `(args, PolicyContext)`; platform context, one employer via `ctx.read_tenant` |
| `books.py` | | the minted book id, attempt counters, sent records, rate locks and run claims, all in `ctx.platform_store` |
| `ids.py` | | request-id and nickname derivation |
| `pricing.py` | | SWIFT fees, platform fees, DUAL thresholds, foreign exchange (FX) sizing |
| `ledger.py` | | planned subledger entries, one partition per employer per period (planned only; nothing writes them to the spine's ledger table) |

The package depends on `awx_common` only. The sandbox transfer driver is
`awx_common.sandbox.make_transfer_advance` and the deduplication margin is
`awx_common.ops.DEDUP_RESEND_MARGIN`. There is no network, AWS or model call in kit code: the
spine runs it.

## Tenants

Kit 6 is a connected-account kit, so every tool caller carries a tenant claim. The batch,
lock, release and read tools run as the payroll operator tenant `k6:operator`: a tenants-table
row with no `connected_account_id`. Each employer child runs as `k6:employer:<code>`,
resolved server-side through the TenantResolver; every tenants row is bound to one kit and
role, so a row of another kit is refused. An employer tenant, or any other tenant, calling a
kit 6 tool is refused (`workflow.require_operator`). `require_employer_tenant` re-checks the
resolved tenant before every bind.

## Payroll shape

Parent `run_payroll` runs as the operator and reads no balance and no rate. Its hashed
`computed` lists every child intent plus a per-employer summary of read-free amounts. Rule 3
makes the spine's engine check that each child is canonically equal to an item of a computed
list; kit 6's `children` list is that list. A child that converts carries `fx_lock`:
the stored quote_id, its exact sell amount and its expiry, read from the platform store. So
the approval binds every amount that leaves an account (rule M3) and rule 1's re-run reads the
same stored quote: a market move between approval and execution cannot make the batch STALE
(`test_a_market_move_after_approval_never_makes_the_batch_stale` moves the fake's rate inside
the approval). A lock that is missing, for another amount, for an earlier attempt (quotes are
single-use), or expiring within 2 minutes leaves that employer without `fx_lock`, and its
child is BLOCKED ("lock rates again"); the other employers run. Nothing is ever summed across
employers. `fan_out` yields one child intent per employer and `child_tenant`
(`k6:employer:<code>`) names its tenant.

Child `employer_payroll` runs under one employer's tenant. Policy: AUTO when the employer's
own wallet covers what is left of this period at the approved sell amounts, BLOCKED otherwise
(SHORTFALL, no lock, another run in flight, or NEEDS_PERSON). Its `computed` is empty. Steps:

1. `snapshot`: list financial_transactions, read balances, list again. When the second list matches
   the first, store the opening balances, those transaction ids and the payouts that already exist,
   keyed by proposal_id, with a `run_token` for this attempt. It mints the book on first use
   and takes the run claim.
2. `validate_beneficiaries`.
3. `create_beneficiaries`: the body carries `request_id`; reconcile finds the beneficiary by
   its `poc-i6-<book16>-<employer>-<contractor>` nickname, paging the list and matching
   client-side because the list does not filter.
4. `fx_conversions`: uses the approved quote_id and sell; refuses an expired lock with nothing
   sent; a conversion already sent under its current attempt is re-emitted unchanged.
5. `payouts`: one `transfers/create` per contractor, explicit `transfer_method`,
   `source_currency` = payout currency. Before the first payout leaves, the funds check below
   runs. In sandbox mode `awx_common.sandbox.make_transfer_advance` moves each payout to SENT,
   then PAID after its hold (rule 10).
6. `platform_fee`: `charges/create` through `bind_platform_operation`, only when every payout
   is PAID; its reads are platform reads.

Postcondition (wallet delta): every payout PAID (a CANCELLED one is reported with
`failure.code` and `details.type`); every conversion readable in this employer's account; and
for every currency `closing - opening - deposits not in the snapshot's id set` equals exactly
what this run did, from read-back objects. It marks the attempt finished and releases the run
claim.

## Funds check and part-paid employers

Before the first payout of an employer child leaves, `build_payouts` reads that employer's
balances and requires, per currency, every payout still to send plus its SWIFT fee (the same
`pricing.payout_cost` table the conversion was sized from), and the platform fee in the
funding currency unless it is already collected. Otherwise nothing is sent (`FundsShort`).

If a payout is still refused after earlier payouts of the same child left, the engine sends
nothing more from that step, runs finality (and, in sandbox mode, the advance) for the
payouts that left, and fails the run with the refusal. The postcondition reports
`PART-PAID, a person decides` with the paid and unpaid contractors, and no platform fee is
charged. The refused payout is not retried in that run; its definitive failure is recorded,
so a later approved run may pay it under the next attempt.

## Request ids

Every id is `derive_request_id("k6", key, step)` (`ids.py`). Every key starts with the minted
book and names the business object:

| Object | Key | Step |
|---|---|---|
| payout | `payout:<book>:<employer>:<contractor>:<period>` | `transfer:<attempt>` |
| conversion | `fx:<book>:<employer>:<period>:<sell>-><buy>` | `conversion:<attempt>` |
| quote | the conversion key | a per-minute step |
| platform fee | `fee:<book>:<employer>:<period>` | `charge:<attempt>` |
| beneficiary | `beneficiary:<book>:<employer>:<contractor>` | includes the period |

The same business action in one book always yields the same request_id, so a rerun of a pay
period re-presents the same ids and can never pay twice.

## Kit rules M1 to M4

M2, book. `books.py` mints a random 32-hex book id in `ctx.platform_store` the first time a
child runs (the snapshot step), and every business key, plus the beneficiary nickname,
starts with it. `ids.py` refuses anything that is not a minted id. Employer, contractor and
pay period come from seed data, so without the book a second demo run on the shared sandbox
would re-derive the first run's request_ids and adopt its payouts. Two books seeded the same
way produce disjoint request_ids
(`test_two_books_seeded_the_same_way_produce_disjoint_request_ids`).

Attempts. Payout, conversion and fee ids end in `:<attempt>`. The postcondition records a
definitive failure (FAILED at create with DuplicateRequest excluded, or a transfer reading
CANCELLED with its failure block) in the platform store; the next approved run uses the next
attempt, with a fresh quote for a conversion.

M1, in flight. OUTCOME_UNKNOWN, an empty read-back or a timed-out finality poll never
advances the attempt. The next run re-emits the same Operation: transfer and fee bodies are
deterministic, and a conversion's quote_id is recorded before it is sent and reused. There is
no other funding path (no `connected_account_transfers`).

M1, window. Every money operation records `first_sent_at` (and the run that sent it) when
first built. An in-flight one older than `DEDUP_SAFE_MARGIN` (6 days, inside Airwallex's
deduplication window of about 7 days) is never re-emitted: the child policy returns BLOCKED
with `NEEDS_PERSON: employer, pay period, request_id`, and the build gate refuses it too and
records it, so every later run for that employer and period stays BLOCKED. The attempt never
advances, so that money cannot be funded or paid another way. Other employers run.

M1, refused re-emission. A refusal counts as definitive only for a first send of this attempt,
and never when retryable (429, a 5xx that was not sent) or OUTCOME_UNKNOWN (the spine's
refused resend and multiple_objects shapes): `workflow.definitive_create_failure`. A 4xx on a
re-emission of an earlier run's unknown send is not a definitive failure (the original may
still land), so no next attempt is taken; the money stays in flight until the original's own
failure state is read or a person decides. `definitive_create_failure` stays as defence in
depth: with a fresh operation store the executor cannot know an earlier attempt was
uncertain (`test_a_refused_resend_stays_in_flight_when_the_operation_store_does_not_know`).

M3, approval binds amounts. The approval binds employer, every contractor line, SWIFT fees,
need per currency, platform fee, and each conversion's exact locked sell amount and quote.

M4, owners. Kit 6 takes no holds. Its per-run state (the opening snapshot) is keyed by
`StepContext.proposal_id`, and the run claim below is owned by the child's proposal_id and
released only by its owner. There is no `on_abandon`.

## Matching objects under a kit request_id

Any object found under one of kit 6's request_ids must match the kit's operation on every
field in `workflow.TRANSFER_MATCH` (amount, currency, beneficiary found by nickname, source
currency, method, reason, reference), `CONVERSION_MATCH` (currencies, buy amount, and the sent
record's sell amount and quote_id) and `FEE_MATCH` (amount, currency, source, reason,
reference). Each list covers the spine endpoint's `match_fields` (`tests/test_match_fields.py`
pins it). The kit never adopts a mismatched object's status, so a foreign transfer can never
read as a paid payout or unlock the fee.

`reason` and `reference` (kit-owned; not in the spine's transfer list, and `reference` not in
its charge list) are compared only when the found object carries them, because the live echo
of those fields is not confirmed. A mismatch the kit sees fails closed before sending
(`RequestIdBoundElsewhere`); a mismatch the spine finds comes back OUTCOME_UNKNOWN
`request_id_reused` and stays money in flight. A by-id NotFound after a send that may have
landed is unknown, never absent.

The read tool `read_payroll_plan` runs as the operator and cannot state a fee charge's
`source`: it checks every other field and reports `fee: "charge found; source not checked by
this read"`. Policy and builds always check the source.

## Attempts and run_token

A run_token names one attempt of one proposal_id. In AWS the execution name is the
proposal_id, so Step Functions never runs two executions of one proposal at once. A
redelivered snapshot task of an unfinished attempt keeps its token; a later run of the same
proposal_id (a re-proposal in the same window, which `local_runner` allows) finds the snapshot
finished and mints a new token, so the earlier attempt's sends count as re-emissions.

## Run claims

One run per employer and pay period at a time. The snapshot takes
`claim/<book>:<employer>:<period>` in the platform store with a conditional create, owned by
the child's proposal_id, and records the payroll's root run (`root_proposal_id`,
`root_action`). The child policy is BLOCKED ("another run of this period is in flight") while
another proposal holds it. The claim's owner passes: a redelivered or resumed child sees its
own proposal_id through `PolicyContext.proposal_id`, and `take_claim` returns "already ours".

The claim is released on every path where the owner's postcondition runs (try/finally, owner
checked) and in the snapshot step before `SnapshotUnstable`. Releasing with money in flight is
safe: that money stays on its request_id, and the next run re-emits it, reconciles it, or
routes it to a person past the margin. A crash that skips the postcondition leaves the claim
held (fail closed); a person releases it with `propose_release_run_claim`.

## Rate locks

`lock_rates` (AUTO): for each employer that pays in another currency, one child under that
employer's tenant creates an FX quote per currency (`validity: HR_1`, the longest validity the
sandbox accepted among MIN_1, MIN_15 and HR_1) and its postcondition stores the quote in the
platform store as the lock for the conversion's current attempt request_id. A quote moves no
money and binds nothing until a person approves the payroll that uses it, which is why this
action alone sets `agent_min_tier=AUTO` (the rule 11 justification).

The lock action keeps an unexpired lock for the same attempt (it may be under approval) until
it has actually expired, so it can never re-quote under an approval that binds the current
lock. Lock expiry is judged in the child policy, so the parent's hash never moves with the
clock. Once a conversion is sent, the approval binds exactly its sent quote, sell and expiry,
never a newer lock, and the build refuses a re-emission that differs
(`QuoteAboveApprovedCap`, nothing sent). Every currency is checked before any is recorded, so
a refusal on one never leaves another recorded but unsent.

With HR_1 quotes the payroll must be approved within about an hour of the lock, or locked
again. Longer validities exist in the fake only.

## Claim release runbook

`propose_release_run_claim` (`release_run_claim`, SINGLE, never below) releases a claim left
by a run that died before its postcondition. The release asks `run_status` for the claim's
root payroll run as `k6:operator`, at proposal (or, from the tool plane, which has no
`run_status`, at recheck), again at rule 1's re-run and again at apply:

| Root status | Result |
|---|---|
| terminal (`RUN_TERMINAL`) | SINGLE: a person approves; re-checked at apply |
| `RUN_RUNNING` | BLOCKED |
| None (purged after Step Functions' 90 days, or a local run with no registry) | BLOCKED; a person with the evidence works in the platform store directly |
| the claim names no root | BLOCKED |
| a refused lookup (a child's id, another tenant's proposal) | raises, so the run fails closed |

The approval binds the claim's owner, the time it was taken and the root, so a claim that
moved, or a root that reads RUNNING again, makes the release STALE. It moves no money and
changes no attempt: money the dead run left in flight stays on its request_id.

A deployed root reads terminal only once it has stopped and can send
nothing more. After a payroll that SUCCEEDED with every child returned, that is its stop time
plus 195 s (the adapter Lambda's 135 s timeout and a 60 s margin). After a FAILED, TIMED_OUT or
ABORTED payroll, or one that lost a child, it is the stop time plus 195 s plus the child
machine timeout (22,155 s: `engine.child_budget_s(EMPLOYER_SPEC)` plus an hour). So a claim
left by a failed payroll is releasable about 6.2 hours after the payroll stops.

## Approval tiers

| Where | Condition | Tier |
|---|---|---|
| parent | every employer has a pricing blocker | BLOCKED |
| parent | one employer's need in one currency (payouts plus SWIFT fees) >= USD 10,000 / AUD 15,000 / GBP 8,000 / EUR 9,000, or any other currency | DUAL |
| parent | otherwise | SINGLE |
| child | pricing blocker, unreadable state, duplicate beneficiary nickname, another run in flight, past the margin, or shortfall | BLOCKED (escalate) |
| child | funded from its own wallet (including "everything already done") | AUTO |

The parent is never AUTO, so a person approves every batch, whether the model proposed it
(origin AGENT, floored to SINGLE anyway) or `awx-deploy trigger` started it (origin SCHEDULE,
keeps the policy tier). Every kit 6 spec uses `ApproverScope.OWN_TENANT`: root proposals run
as `k6:operator`, so the operator tenant's approvers decide them.

## Design requirements

| Requirement | How |
|---|---|
| Provisioning | Out of scope for a pure core; the tenant map is the resolver's |
| One execution and ledger partition per employer | `fan_out` plus `child_tenant`, and `ledger.partition_key` |
| Assert where a conversion landed | the wallet-delta postcondition. A build that drops the `x-on-behalf-of` header is stopped by `check_tenant_scope`; a provider that drops it is caught by the postcondition |
| No cross-employer funding path | the parent reads nothing, the child reads only its own wallet, and the kit has no `connected_account_transfers` call. Property tests check that no other wallet moves and no write names another employer |
| Approval bound to employer, contractors, currencies, fees and amounts | rule M3, above |
| Partial-batch recovery | rerun the same period. Done conversions and the fee are skipped, an unfinished payout is re-presented and driven to terminal, and funding is sized on what is left. A CANCELLED payout is not retried automatically: the postcondition reports its `failure.code` and a person decides |

## Guide ambiguities and the reading taken

Safest reading first.

1. Transfer terminal states: a failed transfer always reads CANCELLED
   ([FAKE-FIDELITY.md](../../packages/awx_spine/FAKE-FIDELITY.md)); FAILED is only a transition
   reply. CANCELLED and FAILED each end the poll as a failure; SENT is never final.
2. SWIFT fees: sized at EUR 12.85 per SWIFT payout, the largest figure in the evidence. The guide documents
   a flat EUR 12.85 for EUR SWIFT payouts of 100 to 4,000 EUR; a sandbox probe charged EUR
   6.21 on 1.00 and 100.00; a live sandbox run had a 2,000.00 EUR SWIFT payout refused
   `balance_insufficient` with 2,006.21 EUR available (the 400 body named no fee). The figure
   sizes the conversion and the pre-send funds check only; the postcondition uses each
   transfer's real cost (`amount_payer_pays`, or `transfer_amount + fee_amount`). SWIFT in any
   other currency is refused (an unpriced corridor fails closed). 12.85 is the documented
   figure, not a measurement at 2,000 EUR: if the sandbox charges more, the funds check stops
   the payouts step before any payout leaves, after the conversion has settled, and the
   employer keeps the converted EUR.
3. FX orientation: `currency_pair` is fixed per pair (EURUSD both ways) and `rate` is second
   per first; `pricing.sell_for` accepts the pair as sell then buy or as buy then sell, and refuses
   any other pair.
4. Platform fee amount: the guide sets none; `pricing.PLATFORM_FEES` (base plus per contractor)
   is an AWS-authored assumption, charged in the funding currency with reason
   `professional_business_services`.
5. No part-paid employer without a person: see "Funds check and part-paid employers".
6. `financial_transactions` with `from_created_at`, `page_num` (from 0), `page_size`,
   `transaction_type: DEPOSIT`, `currency`, `amount` and `has_more` is not confirmed live. The
   kit pages up to 20 pages of 100 and fails closed past that. If the date filter is ignored
   the postcondition still holds, because deposits are matched by id.
7. Rate lock window: HR_1 quotes, so approve within about an hour of the lock.
8. Payouts in a currency are always fully converted; an existing balance in that currency is
   not used.
9. US beneficiary addresses need an ISO state code (`US-NY`); `NY` fails with code 016.

## Test notes

Tests use the fake's public controls (`set_price`, `credit`, `clear_faults`) and its own
`financial_transactions` list. One private edit remains (`_state.request_ids.clear()` in
`test_two_objects_under_one_request_id_block_without_a_new_attempt`): no public API makes the
provider miss a dedupe. Finality reads are by id, so kit 6 sets no `FinalitySpec.select`.

Rule 0: every build (snapshot, beneficiaries, conversions, payouts, fee, lock quotes, claim
release) runs twice per task in `test_redelivery_and_claim_release.py`; every send happens
once and no claim is left held.
