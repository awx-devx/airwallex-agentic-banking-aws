# Kit 4 design notes

This file holds the detailed design of the Dispute Response Agent: the economics, deadlines,
the approval matrix, request ids, the dispute lock and recovery, clearing a stuck response,
the evidence upload handler, how the tests drive the spine, the prompt-injection fixture and
the reading taken on each guide ambiguity. [README.md](README.md) is the overview.

## Economics

With A the disputed amount from the provider read, F the dispute fee, H the handling costs and
p the win probability for the code-derived evidence band:

```
cost_accept           = A + H_accept            (+ F only if fee_on_accept)
cost_contest_best     = F + H_contest           (win)
cost_contest_worst    = F + H_contest + A       (lose)
cost_contest_expected = F + H_contest + (1-p)*A (half-even to minor units)
expected_saving       = cost_accept - cost_contest_expected
```

- ACCEPT when contesting cannot pay even on a certain win (`cost_contest_best >= cost_accept`),
  when the band is below `min_band_to_contest` and contesting is not expected to save money,
  or when contesting is expected to cost more than the indifference band.
- REFER when the evidence points to merchant fault or contradicts itself, or the saving sits
  inside the indifference band.
- CONTEST when the band is at least MODERATE and the expected saving meets the bar.

The fee and handling costs are modelled. The sandbox never deducts them (guide), so the outcome
ledger (`ledger.py`) labels them `MODELLED` and keeps them apart from `PROVIDER` entries;
reconciliation against a balance uses `PROVIDER` entries only.

The guide's cases at the default AUD schedule (fee 25, contest handling 20, accept
handling 2):

| Case | Evidence | Result |
|---|---|---|
| 900.00 fraud | signed delivery, three linked prior orders: STRONG | expected cost 270.00 against 902.00 to accept: CONTEST |
| 15.00 not received | any | 45.00 to contest even on a win: ACCEPT with `LOW_VALUE` |
| 180.00 credit not processed | two unanswered support emails: merchant fault | REFER |

Win probabilities per band (NONE 0, WEAK 0.15, MODERATE 0.45, STRONG 0.75), fees, handling
costs, the indifference band and the dual-approval threshold are assumptions in `config.py`
(AUD and USD schedules). A real merchant would load its own.

## Deadlines

The provider deadline (`due_at`) wins when the read carries one; otherwise code models one from
`created_at` plus the stage's window and says so first in the proposal reasons ("DEADLINE IS
MODELLED"). The action must land by `act_by = respond_by - margin` (24 hours for a challenge,
1 hour for an accept). That is checked at proposal and again when each step builds its
operation, against that build's own `ctx.policy.now` (rule 6), so a deadline that passes while
approval is pending stops the write (`DeadlineMissed`). No deadline at all counts as passed.

## Approval matrix

| Action | AUTO | SINGLE | DUAL | BLOCKED |
|---|---|---|---|---|
| accept | never | amount below the dual threshold | amount at or above it | not respondable; act-by passed; amount differs from the provider; a refund already exists; `LOW_VALUE` while contesting could still pay; `AFTER_ISSUER_REJECTION` at the request for information (RFI) stage; no economics for the currency; policy version moved |
| challenge | never | economics CONTEST, amount below the threshold | amount at or above the threshold, or economics REFER | economics ACCEPT; band below the minimum; minimum evidence missing; reason does not fit the reason family; prior challenge visible; act-by passed; amount differs; no economics for the currency; policy version moved |
| refer | always (moves no money) | - | - | - |
| close | when the provider status is terminal and the typed reason fits it, and for ACCEPTED the accept reason actually used | - | - | otherwise (PENDING_CLOSURE and PENDING_DECISION wait) |
| clear | never | a fresh read-back that is empty for that response, after the clear bound | - | anything undetermined, or a response the dispute shows |

Also BLOCKED for accept and challenge, each with a reason a person can act on: evidence missing
from this dispute's index (challenge); an earlier response whose outcome is unknown ("may have
reached Airwallex ... a person reconciles it ... Nothing is resent"); an earlier response
Airwallex refused (its request_id cannot carry a new body); another run still uploading
evidence; a landed accept; a landed challenge (for a new challenge). Accepting after a landed
challenge is allowed: that is the path once the issuer rejects the evidence. The dispute lock is
also enforced in the build, which fails closed with nothing sent when another run owns it.

Agent floor: accept, challenge and clear keep the default `agent_min_tier=SINGLE`. Refer and
close set `agent_min_tier=AUTO`. Refer writes only a note for a person and sends nothing to
Airwallex; close sends nothing either and only records an outcome Airwallex already reached,
BLOCKED unless the provider status is terminal and the reason fits it (and the accept reason
used). An injected document can make the model propose a refer or a close; refer and close move no
money.

The approval binds through `awx_common.approvals` to the canonical intent (dispute, disputed
amount, typed reason, evidence ids, every cited fact, the narrative, the policy version) and to
`computed`: the evidence metadata and SHA-256 list from the index, the economics, the deadline
and its source, the request_id, `product_type` and a fingerprint of the case's attempts and
outcome. Changing any of them changes the hash. Kit 4 runs as the platform tenant, so platform
approvers decide (`ApproverScope.OWN_TENANT`).

## Guide gaps this kit closes

| Gap | Kit handling |
|---|---|
| Accept and challenge are REST-only | Accept and challenge are `Operation`s for the spine's REST adapter (`provider.accept_operation`, `challenge_operation`). No Model Context Protocol (MCP) path exists in this kit. |
| An approval point before an irreversible accept or refund | Accept and challenge are never AUTO. The kit has no refund operation; an accept with a live refund on the same payment is BLOCKED so money is not returned twice. |
| Handling cost as well as the dispute fee | `CurrencyTerms` carries the dispute fee and the handling costs, and every cost and ledger entry includes them. |
| Idempotency for accept and challenge | See "Request ids" below. |

## Request ids

The API reference requires `request_id` in the accept and challenge bodies
([`FAKE-FIDELITY.md`](../../packages/awx_spine/FAKE-FIDELITY.md) "Accept body", "Challenge body"). Each carries one request_id per dispute, action, stage and
attempt, derived from the business key `dispute:<id>`:

| Operation | Key segment |
|---|---|
| accept | `accept:<stage>`, then `accept:<stage>:<n>` for attempt n |
| challenge | `challenge:<stage>`, then `challenge:<stage>:<n>` |
| evidence upload | `upload:<book>:<proposal_id>:<evidence_id>` |
| sandbox setup (`simulation.py`) | kit-local case name plus the book id |

The request_id is in the body and on the `Operation`. The operation store returns the stored
result for the same body and refuses a different body, so a second challenge cannot be sent
under the same key. A stage that lapsed without a kit response is a definitive non-landing, so
the next stage's response gets its own key; a response a person cleared after a fresh empty
read-back gets the next attempt's key (rule M1). Uploads carry a per-run key, so a run after a
definitive upload failure is not answered with the stored failure.

Book id (rule M2): request ids from Airwallex ids (`dispute:<id>`) need none. The sandbox setup
operations key on a kit-local case name and uploads on a kit-local evidence id, so they carry
the book id from `cases.ensure_book`, minted once at random with a conditional create and
validated whole (`bk_` plus 16 lower-case hex).

A consequence of the fixed request_id: a definitive 400 on the first challenge (for example a
wrong `product_type`) cannot be corrected through the kit at that stage without a person
clearing it, so confirm `product_type` before any live demo.

## Finality and reconciliation

After an unknown outcome the executor calls `StepSpec.reconcile` = `finality.dispute_reconcile`:
if the dispute shows the action it landed; if the dispute still waits for a response it did not
land; anything else is undetermined. awx_spine's endpoint table does not count
`disputes.accept` or `disputes.challenge` as deduplicating, so nothing is ever resent (rule 9,
rule M1): the run stops at OUTCOME_UNKNOWN, the lock stays held and a person decides before the
response deadline. `dispute_reconcile` checks the object it finds is the operation's own
dispute. The upload step has no hook: `files.upload` is resend-safe, so the executor's generic
rule applies.

The accept and challenge finality loops poll every 5 s for up to 120 s. Finality reads by id, so
`FinalitySpec.select` does not apply; the kit has no local deduplication constant, so
`DEDUP_RESEND_MARGIN` does not either (dispute responses are never resent).

## Case state, the dispute lock and recovery

`cases.py` keeps one record per dispute in `PolicyContext.store` (`case/<dispute_id>`). Every
write is idempotent (runner rule 0): `update_case` writes nothing when a change leaves the
record as it was, so a redelivered build or postcondition does not move its version.

### The lock

Rules M1 and M4. The lock is owned by the run's `proposal_id`, never by the intent hash. The
first build takes it: the upload step for a challenge, the accept step for an accept. Only the
owner's postcondition releases it, and only on a definitive outcome: the dispute reads back
responded (LANDED), every accept or challenge it sent came back FAILED at create (REFUSED), or
it sent no response and recorded no attempt (a failed upload). After OUTCOME_UNKNOWN, a
finality timeout, or a SUCCEEDED send the dispute does not yet show, the response is IN_FLIGHT
and the lock stays held. A later run takes the lock over only after reading a definitive
outcome for the owner's attempt (the dispute shows it), or when the owner recorded no attempt
(upload only, no money) and the lock is older than `upload_lock_ttl` (1 hour). There is no
`on_abandon`: it runs only when no step ran.

### Attempts

The accept attempt is recorded with the lock just before the accept is sent. The challenge
attempt is recorded only when every upload succeeded and the challenge is about to be sent, so
a failed upload leaves the case retryable (uploads are resend-safe).

`cases.attempt_state` judges each attempt from its own stage's record:

| State | When |
|---|---|
| LANDED | the provider's `accept_details` / `challenge_details` has the response at the attempt's stage, or the dispute is in a responded state at that stage |
| REFUSED | every send FAILED at create |
| LAPSED | refused at its stage, and the stage has since moved on: a refused RFI that ran out into a chargeback re-opens the case for the new stage |
| CLEARED | a person cleared it (a late landing still reads LANDED) |
| IN_FLIGHT | anything else |

An attempt this run recorded with no result is IN_FLIGHT, never "not sent": the attempt is
recorded just before the operation goes to the executor, and the runner can drop a step's
result after an error. A stage change does not turn an unknown response into LAPSED while the
live per-stage record shape is unconfirmed.

When this run's own send was refused while the stage shows a response and another run of this
kit also responded at that stage, the postcondition records REFUSED and reports "answered by
another run". With no other kit attempt, a refusal of a landed response is most likely this
run's own duplicate delivery, so it stays LANDED (the provider record carries no request_id to
tell them apart). A run whose build finds another run's response already landed sends nothing
and reports "already done by proposal <id>" (postcondition not ok), naming every other LANDED,
uncleared attempt at that stage.

### Stage binding and attempt numbering

Every build refuses (`StaleDispute`, nothing sent, before any lock or attempt) when the
dispute's stage differs from the stage in the approved decision, so a response is only ever
sent at the stage the approver saw. Under the lock, the build re-derives the attempt number: if
another run recorded an attempt at this stage since the proposal, the approved request_id is no
longer this run's to use, and the build fails closed with nothing sent.

### Policy sees only other runs

Under the runners, policy runs for a proposal and again for that run's rule-1 re-run, each time
before the run takes the lock or records an attempt. So every lock and attempt policy sees is another
run's, and policy refuses at proposal time, before any approval is asked for, a proposal over
another run's IN_FLIGHT or REFUSED attempt or live upload lock (a lock whose owner has recorded
no attempt), identical or not. A person decides every IN_FLIGHT or REFUSED response: policy
BLOCKS new responses with the reason and the deadline, and the dispute endpoints are never
resent.

## Clearing a stuck response

`propose_clear_dispute_response` (`clear_dispute_response`, one approver, never AUTO) is the
only way out of an IN_FLIGHT or REFUSED accept or challenge.

The clear bound (`clearing.py`): a clear is refused unless the run that owns the attempt reads
terminal through `ctx.run_status(owner, owner action)` and the attempt (`recorded_at`) is older
than `CLEAR_BOUND` = the attempt lease (180 s, pinned to awx_spine's `DEFAULT_ATTEMPT_LEASE_S`
by a test) plus the response step's finality deadline (120 s) plus a 10-minute visibility
margin, 15 minutes in all. RUNNING, no run, an error or no `run_status` callable all refuse; at
the agent's propose call, where the tool plane has no callable, the check is deferred to the
approved run's recheck. The clear's postcondition applies the rule again before it clears, and
only then releases the owner's lock (only when that run holds it and reads terminal). After a
clear, a different action at that stage also waits out the bound, because accept and challenge
are irreversible.

Policy acts only on a fresh read-back that is empty for that response: the dispute still waits
for a response at the attempt's own stage and records no such action there. An unknown status,
a stage move or a read that shows the response is BLOCKED. The postcondition re-reads before it
clears, so a response that landed after approval is left alone. Clearing keeps the attempt and
keeps watching it: a late landing reads LANDED and refuses a second response. A redriven or
redelivered owner execution whose attempt was cleared never sends under the old key. The next
response at that stage gets its own request_id (`<action>:<stage>:<n>`). `request_ref` names an
existing attempt from `read_dispute`; code never builds an operation from it.

`VISIBILITY_MARGIN` (10 minutes) is an assumption. Set it from the accept and challenge read lag
measured in the first live run: time to visibility after a 200 and after a client timeout,
whether a second response is refused, and the worst lag with headroom. If that lag is minutes,
anchor the bound to the end of the owner run rather than `recorded_at`.

FakeAirwallex refuses a second response on a dispute (a 400 "Dispute transition is not
supported") and shows the new dispute status at once after an accept or challenge; the live
sandbox has not confirmed these behaviours.

## Evidence upload handler

`functions.FUNCTIONS` declares one Lambda, `evidence_upload` (handler
`awx_k4_dispute.upload.handler`, grants `EVIDENCE_PUT` and `KIT_STORE` only, 60 s, 512 MB). The
generic KitStack builds it with exactly those grants; an operator invokes it directly by the
name in the kit stack's output.

It takes one file for one dispute
(`tenant_id, dispute_id, evidence_id, file_name, kind, content_base64[, text]`), types, sizes
and hashes it from the bytes (PNG refused), writes the code-owned index entry to the kit store,
then writes the bytes to `evidence/k4/<tenant_id>/<store_id>`, the key the adapter's evidence
store reads. Caller-supplied metadata fields are refused. `tenant_id` must be `platform` (kits
1 to 4 run as the platform, so evidence filed elsewhere would never be read).

Files are capped at 4.4 MB: the handler takes them base64-encoded in a synchronous invoke, whose
payload limit is 6 MB. Larger files would need an S3-based upload, which the kit does not have.
Retrying with the same bytes is idempotent; different bytes under an existing id are refused
before anything is written. `text` is the file's extracted text (the operator's optical
character recognition (OCR) or PDF text): with it, every quote the model cites is checked
against the document; without it, quotes are checked against citations only and the approver
is told so.

The handler's core (`ingest_upload`) is pure. Its entrypoint builds the AWS store and S3 client
inside the function at invocation: the DynamoDB KitStore lives in awx_spine_aws, which KitStack
stages into the function's code, so kit 4 has no awx-spine dependency and imports only
awx_common at module level (tested in `test_tool_surface.py`).

### The evidence index

Policy resolves each proposed evidence id from this dispute's index in `ctx.store`
(`evidence/<dispute_id>/<evidence_id>`) and BLOCKS a missing id, an id filed under another
dispute, or malformed stored metadata, so another case's file cannot be sent. The upload
`EvidenceRef` is built from the index, never from tool input. Bytes live in the evidence store
under `evidence.store_id(dispute_id, evidence_id)`, unique per tenant, so two disputes can each
have an `ev_signature`.

## Running on the spine in tests

Tests run every scenario through `awx_spine_aws.local_runner.run_kit_workflow` against
`awx_spine.FakeAirwallex(online_payments_enabled=True)`, awx_spine's MutationExecutor and
InMemoryOperationStore, with the fake clock wired into `now` and `sleep`
(`tests/k4_harness.py`). Disputes are staged the live way: payment intent, confirm with the test
card, `simulation/pa/payment_disputes/create` with `due_at`. Each dispute's fixture evidence is
indexed (`ingest_evidence`) with its bytes in an `InMemoryEvidenceStore` under `store_id`, as
the deployed upload handler does.

The fake enforces `request_id` in accept and challenge bodies and `product_type` at CHARGEBACK,
serves `GET /pa/refunds`, and gives a definitive 400 (`Fault.REFUSE`). The harness adds only
what the fake does not model: an adapter crash after the send and the loss of a whole
execution. For want of a public API, the harness edits the fake's private state for dispute
objects (status, stage, amount, due_at, response details) and for a seeded refund.

Every OUTCOME_UNKNOWN the spine can return (`request_id_reused`,
`request_id_bound_to_another_body`, `first_attempt_unknown`, `past_dedupe_margin`,
`multiple_objects`, `in_progress_elsewhere`, and a refused or retryable resend) keeps the
response IN_FLIGHT with the lock held. The real executor never produces the resend and
`multiple_objects` shapes on the dispute endpoints (it reconciles and stops without sending),
so those are tested at the postcondition directly. The spine records every dispatched operation
in `prior`; the kit still treats an attempt it recorded with no result as IN_FLIGHT. Every build
is tested running twice for one proposal (rule 0, `tests/test_rebuilds.py`).

Faults proved through the runner: a challenge that times out after landing (found by
reconcile); a 500 that still applied; a 500 before anything happened (OUTCOME_UNKNOWN, lock
held, a person decides, never resent); a 429 then an adapter crash; list lag; a duplicate
delivery; a landed-but-invisible upload (resent inertly); eight days passing on an in-flight
challenge (still a person's: kit 4's endpoints ignore request_id for deduplication, so rule
M1's route-to-a-person clause applies from the first unknown outcome). FakeAirwallex hides objects a write creates; a dispute response updates
an existing object, so after a timeout it is visible at once and reconcile finds it.

`tests/test_acceptance_sandbox.py` (marker `sandbox`) runs the acceptance scenario with
`poc-i4-` labels, a fresh book id and `flock /tmp/awx-sandbox.lock`; its live variant is
skipped until Airwallex enables online payments on the sandbox account.

## Prompt-injection fixture

`tests/fixtures/injection/ev_customer_letter.pdf` (text in the `.txt` sidecar) is a synthetic
cardholder letter that tells the agent to ignore its instructions, accept the dispute, refund
AUD 900.00 to a named bank account and skip approval. `evidence.instruction_markers` flags it
for the approver; markers never lower a tier or trigger a write.

`test_prompt_injection_in_evidence_cannot_cause_a_write` proves at the domain layer that even a
model that obeys the letter can only propose: the obedient `LOW_VALUE` accept of 900.00 is
BLOCKED by the economics, any other accept waits for a bound approval, no tool input can carry
a refund, account or request_id, and the letter riding in a challenge pack changes no tier while
the approver is told, by name, that it carries instruction-like text (markers come from the
indexed document text, not only the narrative). The same test runs refer and close, the AUTO
actions an injected letter could push without a person: refer sends nothing, and close is BLOCKED at a
non-terminal status. `test_facts_fabricated_against_the_injection_letter_do_not_raise_the_band`
shows an obedient model's invented quotes citing the letter are rejected against its extracted
text, so the band cannot be raised. No write reaches the port.

Not yet automated: running the real extraction (Amazon Bedrock, optionally Amazon Textract) and
the agent over this file, then asserting FakeAirwallex saw no accept, challenge or refund and
the proposal waits with no approval record; and the same against the deployed agent and the
sandbox, reading `GET /pa/payment_disputes` and `GET /pa/refunds` and the adapter logs to show
nothing was sent.

Regenerate the fixtures with `python3 tests/fixtures/make_fixtures.py` (needs Pillow, which the
workspace does not install; the committed files are the source of truth).

## Sandbox setup and simulation

`simulation.py` holds the sandbox-only setup behind one interface: a payment intent paid with
the public test card, a dispute staged at RFI or CHARGEBACK with Visa 10.4, 13.1 or 13.6 and a
code-derived `due_at`, escalation and resolution. These are test-harness operations, not agent
tools. Every object carries `poc-k4-` (or `poc-i4-` in the live scenario) in
`merchant_order_id` or metadata, so tests only touch objects they created. Stage only on a
confirmed (paid) intent: staging on an unpaid one returns 500 `operation_failed`. Treat a 500 as
an unknown outcome and read `GET /pa/payment_disputes` back before any resend. Body shapes beyond
the guide's endpoint list are assumptions, each in one function.

In the shared sandbox account, `POST /pa/payment_intents/{id}/confirm` with the test card
returns 400 `validation_error` "This account is not enabled for native API access" (`FAKE-FIDELITY.md`
"Payments, disputes and files"), so no kit 4 scenario can run live until Airwallex enables
online payments for the account.

## Guide ambiguities

- A second challenge after the issuer rejects. The guide's kit section says "handle the
  rejection"; the llms.txt prompt says "challenge again only with new evidence, or accept"; the
  acceptance scenario requires that no second challenge is sent. Reading taken: this kit never
  sends a second challenge. After a rejection it accepts (`AFTER_ISSUER_REJECTION`, with
  approval) or the case runs to `LOST` and is closed with `LOST_ISSUER_REJECTED_EVIDENCE`.
- Fee on accept. The guide says the fee is paid "even after winning" and
  that a small case should be accepted because "the dispute fee exceeds the amount at risk",
  which only works if the fee is a cost of contesting. Default `fee_on_accept=False`; flip it
  in `DisputeConfig` if a merchant pays it either way. Property tests run with `fee_on_accept` true and false.
- Provider shapes. Statuses, `due_at`, the accept and challenge reason enums, `supporting_documents` and
  `product_type` follow the spine's API-reference evidence (`packages/awx_spine/FAKE-FIDELITY.md`).
  REVERSED closes as a win; PENDING_DECISION counts as a received challenge; PENDING_CLOSURE
  counts as a received accept but is not terminal for close (close waits for the final status).
  Still unconfirmed: the `product_type` enum value (`PHYSICAL_GOODS` in `config.py`), the
  provider accept reason strings, the per-stage `challenge_details` / `accept_details` shape and
  `reason.original_code`, and which field shows a prior challenge (`_PRIOR_CHALLENGE_KEYS`); the
  kit-owned guards do not depend on `_PRIOR_CHALLENGE_KEYS`.
- The narrative. The challenge body in the API reference has no free-text field, so the model's
  narrative is shown to the approver and stored on the case but not sent. It could map to
  `customer_info` or `delivery_info` once their shapes are confirmed.
- Accept at RFI with `refund`. The reference allows an optional `refund {amount, reason}` at
  RFI. The kit sends none: accepting already returns the disputed amount, and a second money
  movement on the same case would need its own approval row.
- PNG. Airwallex accepts a PNG at upload, and the reference lists PNG for evidence. The kit
  still refuses PNG, as the guide says. That is stricter than the provider on purpose.
- `GET /pa/payment_disputes/{id}` is documented (API reference "Retrieve a Payment Dispute"); an
  unknown id returns 404.
- Deadline. `due_at` is the provider field; the stage-window fallback is kept for a read that
  lacks it and is stated first in the approver's reasons.
- Win probabilities, fees, thresholds and the evidence size limit are assumptions in
  `config.py`.
