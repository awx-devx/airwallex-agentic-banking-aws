# Kit 3 design notes

This file holds the detailed design of kit 3: the finality allowlist, request ids, the
duplicate lock and its lifecycle, fees and funding, closure, the operator staging path, the
sandbox flow and the reading taken on each guide ambiguity. [README.md](README.md) is the
overview. Runner rules 0 to 11 and kit money rules M1 to M4 are defined in the
`awx_common.workflow` module docstring (see [docs/workflow-rules.md](../../docs/workflow-rules.md)).

## Exports

| Export | Contents |
|---|---|
| `workflow.SPECS` | `replace_failed_transfer`, `close_incident`, `simulate_transfer_failure`, with `awx_common.sandbox.make_transfer_advance("k3")` wired into the replacement's finality (rule 10) |
| `workflow.build_specs(sandbox_advance)` | the same specs with another driver, or none (`None`) |
| `workflow.STAGING_SPECS` | `send_supplier_transfer`, the operator staging path (not deployed) |
| `tools.TOOLS` | `read_transfer`, `read_incident_assessment`, and `propose_<action>` for each spec in `SPECS` |
| `reads.READS` | `read_transfer`, `read_incident_assessment` |

Kit source imports only `awx_common` and the kit itself: `DEDUP_RESEND_MARGIN` from
`awx_common.ops`, the sandbox driver from `awx_common.sandbox`. Kit 3 sets no
`FinalitySpec.select` (its finality reads by id).

## Endpoints and tenancy

Read allowlist: `GET /api/v1/transfers`, `GET /api/v1/transfers/{id}`,
`GET /api/v1/balances/current`. The staging path also reads `GET /api/v1/beneficiaries/{id}`
(`STAGING_READ_ALLOWLIST`, never the agent's reader). Writes: `POST /api/v1/transfers/create`
and `POST /api/v1/simulation/transfers/{id}/transition`.

Kit 3 runs as the platform tenant, on the sandbox platform account: no operation carries
`on_behalf_of`, `check_tenant_scope` passes for a platform tenant, and the kit store is the
platform partition.

## Finality allowlist

`finality.assess` decides from one transfer read. Only status `CANCELLED` with `failure.code`
90802 (BENEFICIARY_BANK_RETURNED: the beneficiary's bank sent the money back) returns REPLACE.

In the sandbox every read after a failure is `{status: CANCELLED, failure: {code, details:
{type}, message}}`. The simulated failure_type is not echoed, and `details.type` is
`INCORRECT_ROUTING` whatever was simulated ([`FAKE-FIDELITY.md`](../../packages/awx_spine/FAKE-FIDELITY.md) "Transfers"). So the code is the key,
`details.type` and any top-level `failure_type` are ignored, and a top-level `failure_reason`
code is a secondary source that must agree.

| Read | Decision |
|---|---|
| SCHEDULED, PROCESSING, SENT, FAILED | WAIT (FAILED is transient; Airwallex moves it to CANCELLED) |
| IN_APPROVAL, APPROVAL_RECALLED, APPROVAL_REJECTED, APPROVAL_BLOCKED, OVERDUE, PAID | ESCALATE |
| CANCELLED with code 90802 | REPLACE |
| CANCELLED with any other code, no code (a bare cancellation) or codes that disagree | ESCALATE |
| any unknown status | ESCALATE |

91401 SYSTEM_ERROR and 91402 CHANNEL_TIMEOUT are not allowlisted: the sandbox cannot
reproduce them (the fake gives them 90000), and a system error or channel timeout can be
followed by a late credit at the beneficiary, so a replacement could pay twice.

## Request ids

| Payment | request_id |
|---|---|
| replacement attempt n (1 to 3) | `derive_request_id("k3", "transfer:<original id>", "replacement-<n>")` |
| staged supplier payment | `derive_request_id("k3", "<book>:payment:<payment_key>", "original")` |
| simulation transition (store key only; the body carries no request_id) | `derive_request_id("k3", "sim:<transfer id>", "<next status>")` |

The replacement's business key names the transfer being re-paid, never the incident, so two
incidents (or one incident delivered twice) about the same transfer collapse onto the same
request_id, and every path that could replace it builds the identical body. The key is an
Airwallex object id, so it carries no book id: a fresh or lost kit store still maps one
original onto the same request_ids, and the provider's deduplication and the `?request_id=`
check still see the first replacement. M2 covers kit-local keys only.

Book (M2): `book` in the kit store holds a random id minted by the first send's build with a
conditional create. Because `payment_key` is kit-local, the staged payment's request_id includes
it, so a fresh book on the shared sandbox never adopts an earlier run's transfer.

## Duplicate lock

These guards stop a second replacement:

1. The request_id above, shared by every path to the same original.
2. The kit-store lock `lock:<original id>` (`ledger.py`), taken by the build with a
   conditional create before anything is sent and owned by the run's proposal_id (M4).
3. The policy lists `GET /transfers?request_id=` for attempts 1 to 3.

### Lock states

| State | Meaning |
|---|---|
| SENDING | taken by the build; the send has not settled |
| IN_FLIGHT | the send ended OUTCOME_UNKNOWN or was never shown to land: the money stays on this request_id (M1) |
| LANDED | a replacement is listed under this request_id |
| REPLACEMENT_FAILED | the replacement itself ended CANCELLED: a person decides |
| RELEASED | the attempt was definitively refused at its first send, or nothing was sent: the next proposal may use attempt + 1 |

The lock also records `owner` (the proposal_id, never `intent_hash`), `attempt`, `taken_at`
(when the attempt was first sent) and `joined_by`.

### Lifecycle

The owner's postcondition (`workflow._settle_lock`) moves the lock from what it reads:

- LANDED once a replacement is listed, REPLACEMENT_FAILED if that replacement is CANCELLED;
- RELEASED only when Airwallex definitively refused a first send (status FAILED with
  InsufficientFunds, ValidationFailed or LimitExceeded, not a read-back mismatch, and no
  `refused`, `last_error` or `_awx_recovered_from` marker in the body), or when the step never
  dispatched (its key is absent from the step results);
- IN_FLIGHT for everything else: OUTCOME_UNKNOWN, a refused resend, two objects under one
  request_id, a read-back that found another payment, a dispatched step with no recorded result.

A later run that finds an IN_FLIGHT lock, or a listed replacement that is not terminal,
resumes. A run that does not own the lock first joins it (a versioned write adding its
proposal_id to `joined_by`), then re-emits the identical Operation on the same request_id, and
the runner reads back before any resend (rule 9). The owner never releases a lock anyone
joined, because its own evidence ("my send was refused") does not cover a joiner's send; a
join that races the owner's release loses on the version and sends nothing.

A later run may mark a lock LANDED or REPLACEMENT_FAILED only after reading that outcome; only
the owner releases. `on_abandon` is not used, so an abandoned proposal releases nothing.

Two runs can dispatch on one request_id (two runs resuming one attempt). Kit 3 does not rely on
the spine to prevent this: the join rule makes the lock fail closed.

### NEEDS_PERSON flags

The replacement postcondition detail starts `NEEDS_PERSON:`, and the lock stays IN_FLIGHT, for:

- every OUTCOME_UNKNOWN code rule 9 stops on at once: `request_id_bound_to_another_body`,
  `request_id_reused`, `multiple_objects`, `first_attempt_unknown`, `past_dedupe_margin`;
- a step that recorded no result, which means refused before its first send (for example
  NotAuthorised or TenantScopeViolation): the next run resumes it on the same request_id, which
  sends it only if it never left, or stops at the margin;
- a joined lock, seen by the owner or by a joiner: any run that finds a live joined lock moves
  it SENDING to IN_FLIGHT;
- a lock move lost to a concurrent write.

Since the spine reports a refused or retryable resend after an uncertain send as OUTCOME_UNKNOWN
with `body["last_error"]`, and two objects under one request_id as OUTCOME_UNKNOWN
`multiple_objects`, these never look like a definitive refusal. The kit still treats a FAILED
result carrying `last_error`, `refused` or a read-back mismatch as not definitive, as a defence.

### Deduplication margin

The margin is the spine's `DEDUP_RESEND_MARGIN` (6 days, from `awx_common.ops`), on Airwallex's
deduplication window of about 7 days; the executor never resends past it either, so kit and
spine agree. It is measured from the lock's `taken_at`, and for a listed replacement with no
lock (kit store and operation store lost) from the transfer's own `created_at`. An unreadable `created_at`, or no
current time, counts as past the margin (`listed_past_margin` fails closed). Past the margin
nothing is sent and a person decides. For the staged payment, `sent:<payment_key>` records the
first send, and a payment still unseen past the margin goes to a person.

## Approval binding

`computed` carries the replacement request_id, attempt, beneficiary, amount, method, fee,
required funds and reference, so the approval binds to them (M3) and rule 1 turns it STALE if
the original changes. The wallet balance is not in `computed`, because a deposit would STALE a
valid approval; the policy checks it and the build re-checks it.

`build_replacement` re-reads the original and the locks and raises `ReplacementRefused` if
anything changed since approval. If the run's own replacement already exists (a retried build),
or the lock is this run's own live attempt, it re-emits the identical operation.

## Amounts, fees and funding

The replacement copies amount, currency, beneficiary and method from Airwallex's record of the
original. Floats in a read are refused. Cross-currency originals and originals with no
`beneficiary_id` (an inline beneficiary) escalate.

For SWIFT, code budgets one more fee: the original's `fee_amount` when it is positive and in
the wallet currency, else EUR 12.85 from the guide (`SWIFT_FEE_FALLBACK`), else BLOCKED. The
original's fee is not assumed refunded, so a SWIFT incident budgets for the original's fee and
the replacement's fee. The sandbox charged EUR 6.21 (`FAKE-FIDELITY.md` SWIFT fee finding); the
guide's higher figure is kept for the fallback so a payment is never under-funded. A resume is
not funding-checked: if the in-flight send landed unseen it has already been debited, and a
resend under the same request_id cannot debit twice.

## Reconcile

`reconcile_transfer_create` finds a create by request_id; two transfers under one request_id
are `ReconcileUndetermined`, and the engine refuses a found transfer whose amount, currency,
beneficiary or method differ (as `request_id_reused`). The simulation steps reconcile by reading
the transfer's status, because a transition can return 500 and still apply (`[tr3-sent]` in
`FAKE-FIDELITY.md`); transitions ignore request_id, so the runner never resends them.

## Closure

`closure.py`: an incident closes only when the original is CANCELLED with its `failure` object
(code and message) and the replacement is PAID or CANCELLED. The record is
written once to `closure:<original id>`; an identical existing record is fine and a different
one is refused. A replacement that also ends CANCELLED fails the postcondition and goes to a
person.

## Operator staging path

`send_supplier_transfer` stages the demo's original payment. It is not an agent tool and not in
`SPECS`, so `KitStack` builds no state machine for it and the Gateway has no tool for it: an
agent that reads supplier messages cannot pay any amount to any beneficiary. It runs only
through `run_kit_workflow` with `Origin.OPERATOR` (the live acceptance test and its fake twin use
it).

Its policy reads the beneficiary and BLOCKS unless the nickname starts `poc-i3-`, pays USD only,
caps the amount at 10.00 USD, refuses a `payment_key` already paid, and checks funds. It puts
the payee's nickname, name, bank and account last 4 in `computed`, so the approver sees who is
paid and the hash binds it. The request_id is not in `computed`: it carries the book id, which
the first build mints (rule 0 forbids the policy writing it).

Known limit: the `poc-i3-` nickname prefix is the only payee check. Anyone with sandbox access
can create a beneficiary with that nickname, and staging would pay it up to 10.00 USD per
approved run. The approver must check the shown name and account last 4 before approving.

## Sandbox flow

1. `send_supplier_transfer` with `stage_for_incident: true`, run by an operator. The reference
   carries the `-nosettle` marker, so the rule-10 driver never moves the original past SENT.
2. `simulate_transfer_failure`: SCHEDULED or PROCESSING to SENT, then SENT to FAILED with the
   chosen failure_type (the sandbox allows FAILED only from SENT). Reads go straight to
   CANCELLED. Use `BENEFICIARY_BANK_RETURNED`: it is the only type whose code (90802) can be
   replaced.
3. `replace_failed_transfer`. The replacement's reference is `poc-i3-r-<original id>`, without
   the marker, so the driver takes it to SENT then PAID and the postcondition records closure.
4. Without the driver, or if PAID never comes, the replace step times out after 5 minutes, the
   run ends FAILED with "not closable yet" and the lock stays IN_FLIGHT; `close_incident`
   records the closure once the replacement is terminal.

The deployed state machines and `awx_spine_aws.local_runner.run_kit_workflow` run these steps in
the same order. Approval windows: 4 hours for replace, close and staging; 30 minutes for the simulation.

## Live acceptance test

`tests/test_acceptance_live.py` (marker `sandbox`, `AWX_K3_LIVE=1`) holds
`flock /tmp/awx-sandbox.lock` for the whole run, uses a fresh kit store (fresh book) per run,
reuses the `poc-i3-supplier` beneficiary (found across every page by nickname plus account
number, created once if missing) and moves 1.00 USD.

Its `finally` always cleans up under the flock and never proposes a replacement: an original
still in flight goes through `simulate_transfer_failure` (a bank return, refunded), and if that
run is refused, one direct transition on a cleanup request_id (FAILED from SENT, CANCELLED from
SCHEDULED or PROCESSING); a replacement still in flight is driven to PAID. The run record
(`$AWX_K3_SHAPES`) holds the outcome, each cleanup step, the lock state and the replacement's
request_id. It reads the replacement by the id its create returned (or the operation store's
record) when the listing lags, and says what a person does (`person_action`) if anything is
still in flight after cleanup. The test fails if anything is left at SENT. Fake-only tests prove
the cleanup by failing the scenario after SENT.

The sandbox's transfer shapes matched FakeAirwallex: CANCELLED with only `failure: {code:
"90802", details: {type: INCORRECT_ROUTING}, message}`, and `created_at` as
`YYYY-MM-DDTHH:MM:SS+0000`.

## Guide traps and the code that handles them

| Trap | Where |
|---|---|
| SENT before PAID or FAILED in the main flow | `simulate_transfer_failure` runs SENT, then FAILED, and polls to CANCELLED |
| Terminal vocabulary: CANCELLED, never FAILED; key on the failure | `finality.assess` on `failure.code` |
| Terminal original disposition before replacement | `assess`, rule 1 and the build re-check; no override exists |
| Mandatory duplicate lock and closure record | request_id, store lock, list check; `closure_record` and `record_closure` |
| Budget a SWIFT fee for the original and for the replacement | `policy.swift_fee` and the funding checks |

## Guide ambiguities

The safest reading is taken each time.

- Replaceable codes. The guide lists none. Only 90802 is allowlisted: the funds came back,
  nothing about the beneficiary or instruction was wrong, and it is proven live. Everything
  else, including 91401 and 91402 (untestable, late-credit risk), escalates.
- Definitive refusal. Only a FAILED refusal of a first send (InsufficientFunds,
  ValidationFailed, LimitExceeded) releases a lock (see "Lifecycle").
- Six days. The guide says Airwallex deduplicates for 7 days; the API reference pages give no
  window (`FAKE-FIDELITY.md`). The margin is the spine's 6-day `DEDUP_RESEND_MARGIN`.
- PAID is not final. The documented state machine allows PAID to FAILED. A PAID original
  escalates (trace with the supplier) and is never replaced.
- No residual-risk override. A person accepting double-payment risk to replace a non-terminal
  original is not built: it needs its own action, DUAL approval and a risk record.
- A store lock without a transfer. The kit releases a lock only when the step never dispatched
  or Airwallex definitively refused its first send. A dispatched step with no recorded result
  stays IN_FLIGHT and is flagged `NEEDS_PERSON:`, so a person can check the operation record
  early.
- Transfer `reason`. `transfers/create` does not validate it (`FAKE-FIDELITY.md`); `goods_purchased`
  is also on the platform movement list.
- Cross-currency originals and originals with an inline beneficiary escalate.
- A replacement a person makes by hand in the web app is not seen by any lock.
