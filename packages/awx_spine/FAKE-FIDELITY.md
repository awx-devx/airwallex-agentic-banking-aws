# FakeAirwallex fidelity: what the sandbox actually does

Every behaviour `FakeAirwallex` models, with the sandbox evidence behind it. Probed live on
6 Oct 2026 (UTC 04:38 to 05:30) against `api.sandbox.airwallex.com` / `files.sandbox.airwallex.com`,
using the sandbox platform account. Raw request bodies and responses (no secrets; the
payment-intent `client_secret` is redacted) are in `evidence/probe-2026-10-06.log`; search it
for the label in brackets, for example `[tr1-sim-failed]`. A second probe run (6 Oct 06:00 to
06:35) is in `evidence/probe-2026-10-06-r1.log` (labels `r1-*`). The logs keep the sandbox
object ids they recorded (transfer, quote, card, beneficiary) so the evidence stays checkable,
and pseudonymise Airwallex account and legal entity ids as `acct_CONNECTED_A`,
`acct_CONNECTED_B`, `le_CONNECTED_A` and `le_CONNECTED_B`. Objects in the logs carry the labels
those probe runs used; the contract test labels its objects `poc-ct-`. Status `S` =
sandbox-verified, `D` = API reference only (not provokable here), `G` = the hackathon builder
guide only, `A` = AWS-authored stand-in. `S (not in the evidence logs)` marks a sandbox
observation whose request and response were not captured in either log.

Where the guide and the sandbox disagree, the fake follows the sandbox. Disagreements are flagged
**FINDING**.

## Wire format

| Behaviour | Evidence | Fake |
|---|---|---|
| Amounts are JSON numbers in major units; the provider normalises them (`10.0`, `25`, `8.88`). A string amount `"10.00"` is also accepted on `transfers/create` | S `[tr1-create]`, `[tr-string-amount]` HTTP 201 | amounts emitted as int when integral, else normalised Decimal; bodies round-trip through `awx_spine.wire` |
| More decimals than the currency allows: `transfers/create` `transfer_amount: 10.001` -> 400 `validation_failed`, `details.errors[0] = {code: "011", source: "transfer_amount"}` | S `[tr-3dp]` | same |
| `GET /balances/current` returns a top-level JSON array (46 currencies on the platform) | S | the port returns `{"items": [...]}` for any top-level array, in client and fake |
| A stale `x-api-version` on `fx/conversions/create` and `fx/rates/current` -> 400 `incorrect_version` "version is incorrect for endpoint"; `2026-08-21` and no header both work | S `[fx-stale-api-version]`, `[fx-rate-stale-api-version]`, `[fx-rate-current-api-version]` | the client never sends `x-api-version` on any call |

## Errors (normalised in `awx_spine.normalise`, shared by client and fake)

| Provider answer | Endpoint | Normalised | Evidence |
|---|---|---|---|
| 400 `balance_insufficient`, source `transfer` | transfers/create | InsufficientFunds | S `[tr-nsf]` |
| 400 `insufficient_fund`, source `charge` | charges/create | InsufficientFunds | S `[charge-nsf]` |
| 400 `insufficient_fund`, source `transfer` | connected_account_transfers/create | InsufficientFunds | S `[cat-nsf]` |
| 400 `insufficient_funds`, source `conversion` | fx/conversions/create | InsufficientFunds | S `[fx-nsf-cad]` |
| 400 `validation_error` message `amount_above_limit`, source `fx_resolver` (checked before funds: 50,000,000 USD) | fx/conversions/create | LimitExceeded | S `[fx-nsf]` |
| 400 `unconfigured_client_fee` | FX on a just-activated connected account | StateConflict | S `[fx-obo]` |
| 403 `forbidden` | FX on a connected account not yet ACTIVE | StateConflict | S `[acct2-fx-before-active]` |
| 400 `expired_quote` "Quote has expired" | fx/conversions/create with an expired quote_id | StateConflict | S `[fx-expired-quote-2]` |
| 400 `conflict_original_ccy` | quote_id used with other currencies | ValidationFailed | S `[fx-quote-wrong-pair]` |
| 401 `credentials_invalid` "Access Denied" | any call with an unknown `x-on-behalf-of` | Unauthorized | S `[obo-bogus]` |
| 401 `unauthorized` "Admin permissions required" | simulation/issuing/card_transaction_lifecycles/{id}/capture and /reverse | Unauthorized | S `[card-capture-lifecycle]`, `[card-reverse-lifecycle]` |
| 404 `not_found` "Transfer not found" | GET a transfer of another account (platform or other tenant) | NotFound | S `[trobo-get-noobo]`, `[cross-tenant-transfer-get]` |
| 404 `NOT_FOUND` "NOT_FOUND" | GET a card of another tenant; GET /issuing/transactions/{unknown} | NotFound | S `[cross-tenant-card-get]` |
| 500 `operation_failed` | simulation transitions the state machine does not allow; dispute simulation on an unpaid intent | OutcomeUnknown (any 5xx on a write) | S. **A 500 on a write is not proof nothing happened**: `[tr3-sent]` answered 500; a later read showed the transfer SENT (S, not in the evidence logs) |
| 429 | never seen; rate limiting is not provokable | RateLimited (retryable) | fake-only (`Fault.RATE_LIMIT`) |

## request_id idempotency

Rejected requests (4xx) do not consume a request_id: the same `fx/conversions/create` body that
got `unconfigured_client_fee` later succeeded under the same request_id (S `[fx-obo]`,
`[fx-obo-retry-same-rid]`).

| Endpoint | Same request_id, same body | Same request_id, different body | Look-up by request_id |
|---|---|---|---|
| transfers/create | 400 `duplicate_request_id`, `details: {id, request_id}` names the original | identical 400 (body is not compared) | `GET /transfers?request_id=` S |
| fx/conversions/create | 400 `invalid_argument` "request_id '...' has been used in the past", source `request_id`, no id | identical | `GET /fx/conversions?request_id=` S |
| connected_account_transfers/create | 400 `request_id_duplicate`, `details: {source_id, source_type: "transfer_id"}` | identical | `GET /connected_account_transfers?request_id=` S |
| charges/create | 400 `request_id_duplicate`, `details: {source_id, source_type: "charge_id"}` | not probed (same family) | `GET /charges?request_id=` S |
| issuing/cards/create | 400 `invalid_argument` "The provided ID is already associated with a previous request", no id | not probed | none found |
| pa/payment_intents/create | 400 `duplicate_request` "The request ID ... has been used previously." | not probed | none found |
| beneficiaries/create | **ignores request_id**: the same body and request_id sent twice created two beneficiaries (201 both) | | none (`?nickname=` does not filter; S, not in the evidence logs) |
| issuing/cardholders/create | **ignores request_id**: same request_id with a new email created a second cardholder; the same email is refused with 400 `bad_request` "The given email or mobile number is already associated with a cardholder." | | `GET /issuing/cardholders?email=` S (the spine's generic read-back uses it) |
| simulation/issuing/create | **ignores request_id**: two sends created two authorisations | | |
| fx/quotes/create | ignores request_id (a replay returned a new quote_id); request_id is optional | | |

An unknown request_id on every list filter returns `{"items": []}` (S `[list-bogus-rid]`).
Read-back after create was never missed: `?request_id=` found the object 0.65 to 1.9 s after the
create on transfers, conversions, charges and connected account transfers (S `race ...` lines).
The CLI cannot probe faster than that, so the executor still retries the read-back after a
DuplicateRequest three times with a short backoff before falling back to OUTCOME_UNKNOWN.

Executor consequence (runner rule 9): `awx_spine.endpoints` marks only transfers, FX
conversions, connected account transfers, charges, cards and payment intents as deduplicating.
Every other endpoint, and any unknown one, is never resent after an unknown outcome. A read-back
or duplicate resolution is accepted only when the found object echoes the operation's
`match_fields` (amount, currency, counterparty); live, a fresh store sending a transfer's
request_id with 2.00 instead of 1.00 got `duplicate_request_id`, read back the 1.00 transfer and
stopped at OUTCOME_UNKNOWN `request_id_reused`, never FAILED (contract test
`test_transfer_negatives_insufficient_funds_and_duplicate`).

Read-after-write visibility: every `?request_id=` read-back found its object 0.65 to 1.9 s after
the create (S, `race ...` lines; the CLI could not probe sooner). The fake models a lag per
object, driven by its clock (`advance`): `FakeAirwallex(list_lag_s=N)` for every create, or
`inject(op, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=N)` (or `Fault.LIST_LAG`) for one write. A
lagged object is missing from lists, `?request_id=` and GET by id until then, whatever else is
written meanwhile, while deduplication sees it at once. Lags longer than 1.9 s are modelled, not
observed (A). Wire `MutationExecutor(sleep=fake.advance)` and
`poll_until_terminal(clock=fake.clock, sleep=fake.advance)` so the lag elapses in tests.

Deduplication window: the guide says 7 days. The API reference pages checked (transfers, charges,
connected account transfers, cards) say only that request_id must be unique; no window is
documented there. Not testable in a day. The operation store has no TTL. The fake
deduplicates for `dedupe_window_s` (default 7 days, G, not sandbox-verified) on every
deduplicating endpoint; past it the same request_id creates a second object. Whether a
`?request_id=` look-up still finds an object past the window is unknown; the fake keeps finding
it (A).

## Transfers (kits 1, 3, 6)

| Behaviour | Evidence | Fake |
|---|---|---|
| Create returns `status: SCHEDULED`; a GET a few seconds later shows `PROCESSING`; nothing moves further without a simulation call (platform and connected-account transfers alike) | S `[tr1-create]`, `[tr1-list-by-rid]` | SCHEDULED, PROCESSING after `advance(1)` |
| The source amount (`amount_payer_pays`) is debited at create; a CANCELLED transfer is refunded | S balances around `[trobo2-create]` / `[trobo2-failed]` (60 -> 55 -> 60) | same |
| `reason` is **not validated**: `platform_fee_bogus`, `payment_to_supplier`, `wages_salary`, `goods_purchased` and `professional_business_services` were all accepted on US LOCAL, and `platform_fee_bogus` on EUR SWIFT | S `[tr-bad-reason]`, `[tr-reason-*]`, `[tr-swift-bogus-reason]` | any string accepted |
| SWIFT fee: EUR SWIFT of 1.00 and of 100.00 both charged `fee_amount: 6.21 EUR`, `fee_paid_by: PAYER`, `swift_charge_option: SHARED`. **FINDING**: the guide says a flat EUR 12.85. **FINDING**: 6.21 is not a flat fee: in kit 6's live sandbox run a 2,000.00 EUR SWIFT payout was refused `balance_insufficient` with 2,006.21 EUR available, so the fee at that amount is more than 6.21 | S `[tr-swift-bogus-reason]`, `[tr-swift-100]`; the 2,000 EUR refusal is S, not in the evidence logs | EUR 6.21; other currencies 6.21 (A) |
| Simulation body is `{"next_status": ...}` with optional `failure_type` (enum of 32 values from the API reference); a bad value -> 400 `bad_request` source `Invalid value found for "failure_type"`; `failure_type: null` -> 400 `Missing body parameter "failure_type"` | S `[tr1-sim-failed-badtype]`, `[tr3-paid-v2]` | same |
| Allowed: SCHEDULED/PROCESSING -> SENT; SENT -> PAID; SENT -> FAILED; SCHEDULED/PROCESSING -> CANCELLED. Refused with 500 `operation_failed` and not applied: PROCESSING -> FAILED, SENT -> CANCELLED, CANCELLED -> PAID, PAID -> SENT | S `[tr2-failed-from-processing]`, `[r1-cancel-from-sent]`, `[tr1-sim-paid-after-cancel]`; PAID -> SENT `[tr3-sent-after-paid]` is S with the state before the call not logged (the log reads the transfer only as SCHEDULED, before the `tr3-*` calls) | same |
| An allowed transition sometimes answers 500 `operation_failed` and still applies (`[tr3-sent]`, and a contract run on SCHEDULED -> SENT; the reads that showed the new state are not in the evidence logs). Clean-up of a SENT transfer must use FAILED, not CANCELLED | S (not in the evidence logs) | `inject(..., Fault.SERVER_ERROR, applied=True)` |
| Nothing moves past PROCESSING without a simulation call: a SENT transfer stayed SENT for 2 min | S `r1-autosettle-*` lines | `advance()` never moves a transfer past PROCESSING |
| Unexplained once: a `-nosettle` transfer read PAID 62 s after creation while an interrupted contract run and a FAILED clean-up (500, the last `r1-cleanup-fail-*` call) overlapped. The controlled repeat above did not reproduce it | S (the PAID read is not in the evidence logs) | not modelled |
| `awx_spine.sandbox.make_transfer_advance` drove a live transfer SCHEDULED -> SENT -> PAID and held a `-nosettle` transfer at SENT | S contract test `test_make_transfer_advance_live` | `test_transfer_advance_*` |
| `FAILED` transition reply: `status: "FAILED"`, `failure_reason: "<code>: <message>"`, `failure_type: "INCORRECT_ROUTING"` | S `[tr1-sim-failed]` | same |
| Every read afterwards (GET by id, list, read immediately and repeatedly): `status: "CANCELLED"`, **no** top-level `failure_type` or `failure_reason`; instead `failure: {code, details: {type}, message}`. A `FAILED` status is never readable | S `[tr1-get-after-fail]`, `GET immediate 1..3` | same |
| **FINDING**: the failure type is not echoed. `BENEFICIARY_BANK_RETURNED` became `failure.code 90802 "Beneficiary bank returned"`, `ACCOUNT_CLOSED` became `90701 "Account closed"`, no `failure_type` became `99902 "Other"`, and `details.type` was `INCORRECT_ROUTING` every time. Kits must read `failure.code` / `failure.details.type`, not the type they simulated | S `[tr1-sim-failed]`, `[trobo2-failed]`, `[fnt-failed-no-type]` | those three codes; other types get `90000` + title (A) |
| A bare `CANCELLED` transition: reply `status FAILED, failure_reason "simulation", failure_type "PAYMENT_RECALLED"`; reads `CANCELLED` with `failure: {details: {type: PAYMENT_RECALLED}, message: "simulation"}` and no code | S `[tr-str-cancel-from-processing]` | same |
| A connected account's transfer can only be simulated with its `x-on-behalf-of`: without it 400 `bad_request` "Can't find payment ... associated with account ..." | S `[trobo-sim-noobo]` | same |

## FX (kits 1, 6)

| Behaviour | Evidence | Fake |
|---|---|---|
| `GET /fx/rates/current?buy_currency&sell_currency`: `currency_pair` is fixed per pair (EURUSD both ways) and `rate` is **units of the second currency per one unit of the first**, at the client's side of the spread: buying EUR 1.127611, selling EUR (buying USD) 1.116014. No `quote_id`. `rate_details` carries `AWX` and `CLIENT` levels | S `[fx-rate-eurusd]`, `[fx-rate-usdeur]` | synthetic prices (A), same orientation and spread side |
| `fx/quotes/create` returns **HTTP 200** (not 201): `quote_id, client_rate, awx_rate, mid_rate, buy_amount, sell_amount, currency_pair, dealt_currency, valid_from_at, valid_to_at, validity, usage: "MULTI_USE"`. `validity` is **required** (400 `field_required`); `MIN_1`, `MIN_15`, `HR_1` accepted, `BOGUS` -> 400 `invalid_argument` "BOGUS is not a valid type" | S `[quote1]`, `[quote-MIN_1]`, `[quote-HR_1]`, `[quote-BOGUS]`, `[quote-no-request-id]` | same; MIN_30/HR_4/HR_8/HR_24 also accepted (A) |
| A quote_id is accepted again under a new request_id (two conversions from one quote, both SETTLED at the quoted `client_rate`), and with a different amount (3.00 against a 20.00 quote). **The kits' single-use rule must be enforced locally** | S `[fx-with-quote-a]`, `[fx-with-quote-b]`, `[fx-quote-different-amount]` | reuse allowed until expiry |
| Expired quote -> 400 `expired_quote` | S `[fx-expired-quote-2]` | same, after `advance` past `valid_to_at` |
| Conversion `status: SETTLED` at create; `sell_amount = buy_amount x client_rate` rounded half-up to the sell currency (10 EUR x 1.127787 = 11.28 USD) | S `[fx1-create]` | same |
| On a connected account the conversion lands in that account's wallet only with `x-on-behalf-of` | S `[ca1-balances-after-fx]` | same |

## Connected accounts and platform money movement (kits 5 to 8)

| Behaviour | Evidence | Fake |
|---|---|---|
| Connected accounts are enabled: `accounts/create` -> 201 `acct_...` `CREATED`; `submit` (empty body) -> `SUBMITTED`; `simulation/accounts/{id}/update_status {"next_status": "ACTIVE", "force": true}` -> `ACTIVE` | S `[acct1-create]`, `[acct1-submit]`, `[acct1-activate]` | same |
| FX before ACTIVE -> 403 `forbidden`; for a few minutes after -> `unconfigured_client_fee`; worked about 8 minutes after activation | S | `fx_ready_delay_s` (default 180, A) |
| `x-on-behalf-of` on balances, beneficiaries, transfers, FX, cardholders, cards and card simulations scopes the call to that account | S `[acct1-balances-obo]`, `[ben2-obo]`, `[trobo-create]`, `[cardholder2-obo]`, `[card2-obo]` | same |
| (a) `connected_account_transfers/create` names the account in body `destination`; `charges/create` in body `source`. Both run as the platform | S `[cat1-create]`, `[ch1-create]` | `awx_common.tenancy.PLATFORM_BODY_FIELDS` |
| (a) `charges/create` **with** `x-on-behalf-of` -> 400 `validation_failed` "Permission denied." | S `[charge-with-obo-header]` | same |
| (a) **FINDING, security**: `connected_account_transfers/create` **with** `x-on-behalf-of: A` is not ignored. It makes A the payer: `destination: A` -> "Not allowed to do transfer when source and destination account are the same."; `destination: B` -> 201 and moved 1.00 USD from connected account A to connected account B | S `[cat-with-obo-header]`, `[cat-obo-A-dest-A2]` | same. `AirwallexClient.execute` refuses any charges/create or connected_account_transfers/create Operation that carries `on_behalf_of` (FAILED TenantScopeViolation, nothing sent), on top of `bind_platform_operation` |
| (b) `reason` on charges/create and connected_account_transfers/create: the first validator lists 24 values; `settlement` and `commission` are on that list but then fail a second validator (400 `validation_failed`). Accepted: `goods_purchased, professional_business_services, technical_services, freight, taxes, transfer_to_own_account, education_training, loan_credit_repayment, investment_proceeds, other_services, medical_services, donation_charitable_contribution, personal_remittance, living_expenses, investment_capital, pension, construction, business_expenses, wages_salary, travel, family_support, real_estate`. Verified accepted live: goods_purchased, professional_business_services, other_services (charges); wages_salary, goods_purchased, professional_business_services (transfers). `payment_to_supplier` and `platform_fee` are not valid here | S `[cat-bad-reason]`, `[ch-*]`, `[cat-*]`, `[ch-settlement]`, `[ch-commission]`, `[cat-settle]` | same two-stage validation |
| Validation order: `request_id`, then `reference` (`field_required`), then `reason` | S `[cat-no-reference]`; the full order is S (not in the evidence logs) | same |
| Create returns `status: NEW`, `fee: 0`, ids `tr_...` / `ch_...`; `SETTLED` within 2 s (GET by id and list) | S `[cat1-get]`, `[charge-get]` | NEW, SETTLED after `advance(1)` |
| (d) Terminal statuses: the API reference lists `NEW, PENDING, SETTLED, SUSPENDED, FAILED` for both and documents `failure_reasons` (e.g. `WATCHLIST_RESTRICTION_PAYER`) on connected account transfers. Treat `SETTLED` as success and `FAILED` as failure; `SUSPENDED` needs a person. A failed charge or transfer cannot be provoked in the sandbox (insufficient funds is refused synchronously and creates nothing) | D; S for NEW and SETTLED only | NEW -> SETTLED only |
| Money moves at create (platform balance down, destination up) | S balances | same |

## Issuing (kits 2, 5)

| Behaviour | Evidence | Fake |
|---|---|---|
| Cardholder: `{"type": "DELEGATE", "email": ...}` -> 202 `status: READY` at once; missing email -> 400 `invalid_argument` "email is mandatory"; ignores request_id (see above) | S `[cardholder1-delegate]`, `[cardholder-rid-*]` | same; INDIVIDUAL is PENDING then READY after 2 s (G) |
| Card create body: `request_id, cardholder_id, created_by, form_factor: VIRTUAL, is_personalized: false, program: {purpose: COMMERCIAL}` (**`program` is required**: 400 "program is mandatory"), `authorization_controls: {allowed_transaction_count: MULTIPLE, allowed_currencies: [..], allowed_merchant_categories: [..], transaction_limits: {currency, limits: [{amount, interval}]}}`, intervals `PER_TRANSACTION`, `MONTHLY`, `ALL_TIME` verified. Reply 202, `card_status: ACTIVE`, `program.type: DEBIT` | S `[card1-create]`, `[card1-create-v2]`, `[card2-obo]` | same |
| `GET /issuing/cards/{id}/limits` -> `{currency, limits: [{amount, interval, remaining}]}`; a PENDING authorisation already reduces `remaining` on cumulative limits | S `[card1-limits]` | same; DAILY/WEEKLY/MONTHLY use rolling windows (A) |
| Authorisation simulation `{card_id, transaction_amount, transaction_currency, merchant_category_code, merchant_info}` -> 201 always, `process_result APPROVED/DECLINED`, `failure_reason`. 100.00 on a 100 limit clears; 100.01 -> `LIMIT_EXCEEDED`; EUR -> `CURRENCY_NOT_ALLOWED`; MCC 5812 -> `MERCHANT_CATEGORY_NOT_ALLOWED`; a 60.00 auth past ALL_TIME 150 -> `LIMIT_EXCEEDED` | S `[card-sim-*]` | same |
| **FINDING**: limits are checked **before** funds. On a connected account with 65 available, 70.00 on a 50 limit gave `LIMIT_EXCEEDED`, not insufficient funds; 45.00 with 25 available gave `INSUFFICIENT_FUNDS`. The guide says insufficient funds comes first | S `[card2-sim-over]`, `[card2-sim-nsf]` | limit before funds |
| An approved authorisation reserves funds (`available` down, `reserved` up, `total` unchanged) | S `[ca1-balances-after-fx]` | same |
| `single_phase: true` answers `type: CLEARING` whether approved or declined; 0.01 on a 0.01 PER_TRANSACTION limit approves, 0.02 is `LIMIT_EXCEEDED`; nothing is reserved | S `[r1-single-phase-0.01]`, `[r1-single-phase-0.02]` | same |
| `POST /issuing/cards/{id}/update {"authorization_controls": {...}}` replaces the controls (200, `card_status` unchanged) | S `[r1-card-update-limits]` | merges controls |
| A card authorisation on another connected account's card | not probed | 400 `card not found`, no write (A) |
| Connected-account card simulated **without** `x-on-behalf-of`: 400 `bad_request` "Transaction could not be created", **and the authorisation is created anyway** (PENDING, funds reserved) | S `[card2-sim-noobo]` + transaction list | same |
| **FINDING**: capture and reverse. The guide's `/simulation/issuing/{id}/capture` -> 400 "Use the card_transaction_lifecycles path for this API version"; the lifecycle paths -> 401 "Admin permissions required" with this API key. `single_phase: true` gives an approved `CLEARING` in one call and is the workaround | S `[card-capture]`, `[card-capture-lifecycle]`, `[card-sim-single-phase]` | 401 unless `FakeAirwallex(admin_key=True)` |
| provider_id of `issuing.simulate_authorization` is the simulation's `card_transaction_id`; to get the id `GET /issuing/transactions/{id}` takes, use `awx_spine.issuing.find_card_transaction` (joins on `card_transaction_data.card_transaction_id`; verified live in the contract test) | S | same |
| `GET /issuing/transactions` pages with `page_num` from 0 and `page_size` of at least 10 (5 gives 400 `invalid_argument` `BELOW_MIN_PAGE_SIZE`), with `has_more`; `find_card_transaction` pages until found | S (not in the evidence logs: follow-up GETs on the fixture card, 26 items in pages of 10, 10 and 6) | same |
| charges and connected account transfer list items echo `amount, currency, source/destination, reason, request_id` (the read-back match fields) | S `[cat-list-by-rid]` (connected account transfers); the charges list read is S (not in the evidence logs) | same |
| `GET /issuing/transactions?card_id=`: oldest first; `transaction_type AUTHORIZATION/CLEARING`, `status PENDING/FAILED/APPROVED`, negative amounts. `GET /issuing/transactions/{transaction_id}` works, but the simulation's `card_transaction_id` is **not** that id (404); join on `card_transaction_data.card_transaction_id` | S `[issuing-txn-get-by-*]` | same |

## Payments, disputes and files (kit 4)

| Behaviour | Evidence | Fake |
|---|---|---|
| `pa/payment_intents/create {request_id, amount, currency, merchant_order_id}` -> 201 `REQUIRES_PAYMENT_METHOD` | S `[pi1-create]` | same |
| **BLOCKER**: `payment_intents/{id}/confirm` with the test card -> 400 `validation_error` "This account is not enabled for native API access". Online payments must be enabled by Airwallex before kit 4 can run live | S `[pi1-confirm]` | refused unless `FakeAirwallex(online_payments_enabled=True)` |
| `simulation/pa/payment_disputes/create` needs `payment_intent_id` and `due_at` (400 `Missing body parameter "due_at"`); with `stage`, `reason_code`, `due_at` on an unpaid intent -> 500 `operation_failed` | S `[dispute-sim-unconfirmed*]` | same; on a paid intent creates a dispute (G) |
| `GET /pa/payment_disputes/{id}` is a documented endpoint (API reference "Retrieve a Payment Dispute"); an unknown id -> 404 `not_found` "The requested endpoint does not exist [...]" | D; S for the 404 `[dispute-get-unknown]` | same |
| Dispute statuses: `REQUIRES_RESPONSE, CHALLENGED, ACCEPTED, REVERSED, WON, LOST, PENDING_CLOSURE, EXPIRED, PENDING_DECISION`; stages `RFI, PRE_CHARGEBACK, CHARGEBACK, PRE_ARBITRATION, ARBITRATION`; response deadline field `due_at` | D | REQUIRES_RESPONSE, CHALLENGED, ACCEPTED, WON, LOST |
| Accept body: `request_id` (**required** per the API reference), `reason` (`AGREEMENT_REACHED_WITH_CUSTOMER, CUSTOMER_RELATIONSHIP_MAINTENANCE, LOW_VALUE_TRANSACTION, VALID_CUSTOMER_DISPUTE, COLLABORATION_ACCEPTED_MANUAL, OTHERS`), `description`, `accepted_by`, `refund {amount, reason}` at RFI | D | reason checked |
| Challenge body: `request_id` (**required** per the API reference), `reason` (`CUSTOMER_WITHDRAWN, CUSTOMER_REFUNDED, PRODUCT_RECEIVED, PURCHASE_HISTORY, NOT_ENTITLED, SEPARATE_PRODUCT, AUTHENTIC_PRODUCT, OTHER_REASONS`), `supporting_documents.documents[{type: PRIMARY/ORDER/CUSTOMER/OTHER, file_ids}]`, `customer_info`, `delivery_info`, `product_type` (required at CHARGEBACK), `product_description`. **FINDING**: the reference says accept and challenge require a request_id. The spine sends bodies as built, so the kit decides | D | supporting document required |
| A second challenge | not documented, not provokable | 400 `validation_error` "Dispute transition is not supported" -> StateConflict (A, from the guide's escalate wording) |
| Dispute actions and account reads by another connected account | not probed | 404 (A, matching the observed cross-tenant 404s on transfers and cards) |
| Escalate from CHARGEBACK -> `validation_error` "Dispute transition is not supported"; PRE_CHARGEBACK auto-accepts | G | same |
| Files: multipart `POST /api/v1/files/upload` on the files host -> 201 `{file_id, filename, notes, object_type: "file", size, created}`. **FINDING**: PNG was accepted at upload (the guide says Airwallex rejects PNG; the reference lists JPG, PNG, WEBP for evidence) | S `[upload jpg]`, `[upload png]` | same; the fake rejects PNG at challenge per the guide (G) |

## Fake-only behaviour (AWS-authored, marked A)

- FX limit `FX_LIMIT_USD = 1,000,000`: the sandbox refused 50,000,000 USD with
  `amount_above_limit`; the real threshold is unknown.
- `quote_id` unknown to the account: 400 `invalid_argument` "quote not found" (not probed).
- `accounts/{id}/update` drops `business_person_details` (G, from the guide).
- Money moves to a connected account that is not ACTIVE (not probed).
- Cumulative card limits convert other currencies with the synthetic prices; DAILY, WEEKLY and
  MONTHLY are rolling windows.
- `x-on-behalf-of` on charges/create or connected_account_transfers/create is refused with
  TenantScopeViolation before anything is "sent", exactly as `AirwallexClient` does. The raw
  sandbox behaviour (charges "Permission denied.", connected account transfers paid by the
  header's account) is behind `FakeAirwallex(raw_platform_headers=True)`.
- Faults: `INSUFFICIENT_FUNDS` only on the five writes with a known sandbox answer (on the card
  simulation it is a 201 DECLINED `INSUFFICIENT_FUNDS`, applied at the funds step, so a limit,
  currency or MCC decline still comes first, as in `[card2-sim-over]`; `transaction_failure_reason`
  stays an unconditional override); `SERVER_ERROR` is not applied unless `applied=True`, which
  the sandbox showed only on simulation transitions, so using it on a create is an
  extrapolation; files.upload honours every write fault (DUPLICATE_DELIVERY stores two files).
- Correlation ids are deterministic, so two fresh fakes return equal results.
- Simulations (transfer transitions, card authorisations and captures, deposits into a hidden
  Global Account, dispute actions, and dispute creation on a hidden payment intent) refuse an
  object that is still hidden by a lag, exactly as if it did not exist. Whether the sandbox lets
  a simulation act on a not-yet-listed object is unknown.
- A non-numeric `page_num`/`page_size`, or a negative `page_num`, is 400 `invalid_argument`
  (ValidationFailed). The sandbox answer is unknown; only `page_size` below 10 was observed.

## Visibility lag, test controls and movement outcomes

| Behaviour | Evidence | Fake |
|---|---|---|
| A landed object that is not yet visible is NotFound on GET by id (and missing from lists) | A: the sandbox listed every object within 1.9 s and no by-id read was ever seen to miss; longer lags are modelled, not observed | `TIMEOUT_AFTER_SUBMIT` / `LIST_LAG` with `visible_after_s`, `list_lag_s`. Kits treat it as unknown |
| `set_price`, `credit`, `reserve` | A: test controls, no sandbox counterpart (prices are synthetic) | public methods |
| A platform movement ending SUSPENDED, PENDING or FAILED; FAILED returns the money | D: the API reference lists these statuses; the sandbox could not provoke them | `set_movement_status(request_id, status)`, `inject(op, Fault.END_AS, final_status=...)`. Never takes the destination below zero (see Fake limits) |
| A definitive 400 on any write | A: shape `{code, message}`, code chosen by the test (default `validation_failed`) | `inject(op, Fault.REFUSE, code=...)` |
| Transfer and other ids unique across fake instances | A | `instance_id` (random unless fixed) |
| `GET /beneficiaries?nickname=` filters | **contradicts S**: a list read after the `r1-ben-*` probes showed `?nickname=` did NOT filter the sandbox list (S, not in the evidence logs) | off by default, as live (the full list comes back); `FakeAirwallex(nickname_filter=True)` opts in. Kits must match client-side |
| Dispute accept and challenge require `request_id`; challenge at CHARGEBACK requires `product_type` | D: API reference | 400 `validation_error` |
| Accepting a dispute at RFI creates a full refund; `GET /api/v1/pa/refunds?payment_intent_id=` lists it | D: API reference (refund on RFI accept); the sandbox answered 200 with an empty list to `GET /pa/refunds` (S, not in the evidence logs) | `refunds` kind |
| `GET /api/v1/financial_transactions`: deposits with id, transaction_type, currency, amount, source_id, created_at; `from_created_at`/`to_created_at`, `currency`, `page_num`/`page_size` | A: the real shape is not recorded by a live read (a probe saw 200 with items; not in the evidence logs) | deposits only |
| `GET /api/v1/balances/history`: every change to the AVAILABLE balance with transaction_type, amount, running balance, source_id, posted_at; `from_post_at`/`to_post_at`, `currency`, paging | A: not yet checked against a live read | ledger of opening balances, deposits, transfers and their reversals, conversions, platform movements and reversals, card authorisations, purchases and reversals, `reserve`, `credit` adjustments; its last running balance equals `/balances/current` available |

### Fake limits

Where the fake does not know what Airwallex does, it refuses or stays conservative rather than
invent a result. A kit that passes here because of one of these limits must still be checked
live.

- A FAILED platform movement whose destination no longer holds the amount (it was spent): the
  live answer is unknown (A). `set_movement_status(..., "FAILED")` raises ValueError, and
  `Fault.END_AS` FAILED ends the movement SUSPENDED with no reversal. Neither invents a negative
  balance.
- The refund created by accepting a dispute at RFI moves no balance and posts no ledger row,
  because the fake's payment intents never credit the merchant's wallet (acquiring settlement is
  not modelled). A kit that reconciles refunds against `/balances` or `/balances/history`
  cannot be tested here.
- `/balances/history` follows available only: a capture moves reserved, not available, so it
  posts no row; reserved changes are visible only in `/balances/current`.
- `financial_transactions` lists deposits only.

## Other

| Behaviour | Evidence | Fake |
|---|---|---|
| `simulation/deposit/create {global_account_id, amount, payer_name}` -> 201 `PENDING`, balance posted at once (AUD 10000000 -> 10000010) | S `[deposit1]` | same |
| `platform_reports/create` validates `type` first | S (not in the evidence logs) | type and file_format required; COMPLETED after 5 s (G) |
| US beneficiary `state` must be ISO (`US-NY`); `bank_account_category` exactly `Checking`/`Savings` | S: the fixture beneficiary was created with `US-NY` and `Checking` (`[r1-ben-create]` uses the same values); `checking` fails in the contract test; `NY` failed in an earlier sandbox run (not in the evidence logs); G | 016 for both |

## Scoped or read-only API keys

Airwallex documents **scoped API keys** (previously "restricted"): per-resource **Read** or
**Write** permissions, where Read grants GET endpoints only and Write grants GET and POST. They
are issued in the web app under Settings > Developer > API keys > New scoped API key, and a key linked to several
accounts needs an `x-login-as` header at login. Source: Airwallex docs, "Manage API keys"
(developer-tools/api/manage-api-keys). So the reader Lambda can hold a read-only scoped key and
lose write capability entirely. The key in this sandbox is evidently not an admin key: the card
lifecycle capture/reverse simulations return "Admin permissions required".

## Executor behaviour that depends on this evidence

- Unknown outcome (timeout, any 5xx on a write, or a record still PENDING after a send): wait,
  then reconcile (the step's hook, else read-back). Resend the identical Operation only when the
  read-back shows it did not land and the endpoint deduplicates; otherwise OUTCOME_UNKNOWN.
  Never a new request_id. files.upload may be resent (an inert duplicate file).
- DuplicateRequest: resolve to the original via read-back (bounded retry) and check it matches;
  without a read-back it is OUTCOME_UNKNOWN for the kit's reconcile (cards, payment intents).
- Redirects are never followed (a 3xx is a non-retryable failure), so credentials cannot leave
  the sandbox hosts.
- Cardholders: generic read-back by `email`, because create ignores request_id.
- Card authorisation simulations ignore request_id; the operation store is the only guard
  against a second authorisation.
