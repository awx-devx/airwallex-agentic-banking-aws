# awx_spine

The pure-Python Airwallex layer of the spine: the sandbox client, the mutation executor and its
operation store, finality polling, and `FakeAirwallex`, an in-memory sandbox with fault
injection. It has no AWS dependency. `awx_spine_aws` wraps it in Lambda handlers and DynamoDB
stores, and every kit's tests run against `FakeAirwallex`. The sandbox evidence behind each
modelled behaviour is in [FAKE-FIDELITY.md](FAKE-FIDELITY.md); the raw probe logs are in
`evidence/`. See also [docs/architecture.md](../../docs/architecture.md) and
[docs/workflow-rules.md](../../docs/workflow-rules.md).

## Modules

| Module | What it holds |
|---|---|
| `client` | `AirwallexClient` (`AirwallexPort` and `FileUploadPort`): the only code that sends a request to Airwallex. Sandbox hosts only, login and token cache, one refresh on 401, narrow retry rules |
| `credentials` | `EnvFileCredentials`, `EnvVarCredentials`, `StaticCredentials`; values never appear in errors, `repr` or `str` |
| `endpoints` | per-endpoint knowledge: id and status keys, whether the provider deduplicates on request_id, how to read an object back, and the fields a found object must echo |
| `executor` | `MutationExecutor`: runs one `Operation` at most once whatever the network does (runner rule 9) |
| `store` | `OperationStore` protocol, `InMemoryOperationStore`, `RequestIdReuse`, `body_hash` |
| `finality` | `poll_until_terminal`: polls an object until its status is in the kit's `FinalitySpec` terminal set |
| `normalise` | maps HTTP answers onto `awx_common.errors` and `OperationResult`, shared by client and fake |
| `fake` | `FakeAirwallex` and `Fault`: in-memory sandbox for kits 1 to 8 with simulated time |
| `issuing` | `find_card_transaction`: joins a card simulation's `card_transaction_id` to its `/issuing/transactions` item |
| `sandbox` | re-exports the sandbox transfer driver `make_transfer_advance` from `awx_common.sandbox` (runner rule 10) |
| `redact` | scrubs credentials and bearer tokens from anything logged or raised |
| `transport` | the HTTP `Transport` seam; `TransportError.sent` says whether a request may have left |
| `wire` | exact Decimal JSON: floats and non-finite numbers are refused |
| `testing` | a scripted `Transport`, fake credentials and operation helpers for tests |

## Key behaviours

- Record before send. `store.begin(op)` writes the operation record before anything is sent,
  and `record_attempt` marks it PENDING immediately before each send. A PENDING record with
  attempts means a send may have left and was never completed (`InMemoryOperationStore`,
  `MutationExecutor.run`).
- Request-id reuse. Every retry reuses the stored request_id and the identical body. The same
  request_id with a different body raises `RequestIdReuse` before any network call
  (`store.begin`, `body_hash`).
- Reconcile before resend. After an unknown outcome, the executor reads the object back (the
  step's reconcile hook, else the endpoint's generic read-back) before anything else. It
  resends the identical operation only when the object has not landed and the endpoint
  deduplicates on request_id (`endpoints.EndpointSpec.dedupes`); otherwise it stops at
  OUTCOME_UNKNOWN. It never mints a new request_id.
- Read-back match. A found object counts as this operation's result only when it echoes the
  operation's amount, currency and counterparty (`endpoints.mismatched_fields`). A mismatch is
  OUTCOME_UNKNOWN `request_id_reused`, never FAILED, because the object may be this kit's own
  earlier body sent through another store.
- Uncertain sends. From the first unknown outcome on, `OperationRecord.may_have_landed` is set
  and never cleared. While it is set, every run reconciles first, and a refusal, a 429 or a
  request that never left is stored as OUTCOME_UNKNOWN with the error in `body["last_error"]`.
- Deduplication margin. Airwallex deduplicates a request_id for about 7 days, so no resend
  follows once the first attempt is older than `DEDUP_RESEND_MARGIN` (6 days), or when its age
  is unknown (`executor.PAST_MARGIN_CODE`, `FIRST_ATTEMPT_UNKNOWN_CODE`).
- Attempt lease. A PENDING attempt younger than `DEFAULT_ATTEMPT_LEASE_S` (180 s, longer than
  the deployed adapter Lambda's timeout) may still be in flight elsewhere: the executor returns
  OUTCOME_UNKNOWN `in_progress_elsewhere` without a read, a send or a write.
- Verified evidence upload. A `files.upload` operation carries an `EvidenceRef`; the executor
  loads the bytes, checks size and SHA-256, and uploads. A duplicate upload is an inert file,
  so it may be resent without a read-back.
- Platform header guard. `AirwallexClient` refuses `x-on-behalf-of` on `charges/create` and
  `connected_account_transfers/create` (the sandbox treats the header as the payer), and
  refuses any malformed account id before any HTTP call.
- Finality polling. `poll_until_terminal` never treats a create response as final, tolerates
  retryable read errors until the deadline, and applies an optional `FinalitySpec.select` to
  pick the operation's row out of a list read.
- Fault injection. `FakeAirwallex.inject(op_name, Fault.X, ...)` models timeouts after submit,
  duplicate delivery, stale reads, 429s, 5xx errors (optionally applied), endpoint-specific
  insufficient funds, definitive refusals, platform movements ending SUSPENDED, PENDING or
  FAILED, and list lag. Time only moves through `advance()`; wire
  `MutationExecutor(sleep=fake.advance)` so backoffs and lags elapse in tests.

## Tests

| Topic | Files |
|---|---|
| Client, credentials, wire format | `tests/test_client.py`, `tests/test_credentials_wire.py`, `tests/test_on_behalf_of_validation.py` |
| Executor and store | `tests/test_executor.py`, `tests/test_uncertain_sends.py`, `tests/test_dedupe_margin_and_lease.py`, `tests/test_crash_recovery.py` |
| Finality | `tests/test_finality.py`, `tests/test_finality_select.py` |
| FakeAirwallex | `tests/test_fake.py`, `tests/test_fake_controls.py`, `tests/test_fake_visibility_and_dedupe.py`, `tests/test_sandbox_fidelity.py` |
| Cross-cutting contract | `../../tests/contract/` |

Everything under `packages/awx_spine/tests` is offline. Run it with the rest of the offline suite:

```
uv run --all-packages pytest packages/awx_spine tests/contract -m "not sandbox and not synth and not live"
```

`tests/contract/test_sandbox_contract.py` carries the `sandbox` marker and calls the real
Airwallex sandbox through `AirwallexClient` and `MutationExecutor` (`make sandbox-test`; the
contract `conftest.py` skips it unless the marker expression asks for it or `AWX_SANDBOX=1`).
It reads credentials through `EnvFileCredentials` and expects long-lived fixtures in the sandbox
account: two ACTIVE connected accounts, a platform beneficiary, a cardholder and a card, named by
`AWX_CONTRACT_ACCT_A`, `AWX_CONTRACT_ACCT_B`, `AWX_CONTRACT_BENEFICIARY`,
`AWX_CONTRACT_CARDHOLDER` and `AWX_CONTRACT_CARD`. Set those to your own sandbox account's
fixture ids; the module skips, naming the missing variables, until all five are set. Every
object a run creates carries a `poc-ct` label (`poc-ct-<hex>` in references and nicknames,
plain `poc-ct` in the merchant, card and file names), so its objects can be found in the
sandbox by that label. A run issues no card and leaves nothing reserved.
