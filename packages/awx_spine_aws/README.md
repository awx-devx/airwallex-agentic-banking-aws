# awx_spine_aws

The AWS runtime of the spine. It holds the workflow engine that both the Step Functions
`KitWorkflow` state machine and the in-process `local_runner` drive, the Lambda handlers the
AWS Cloud Development Kit (CDK) app deploys (kit step, mutation adapter, reader, approval API,
Cognito pre-token trigger), the Amazon DynamoDB stores, the AWS Secrets Manager credentials
provider, the Amazon S3 evidence store and the `awx-deploy` operator command-line interface
(CLI). It builds on `awx_common` (the contract) and `awx_spine` (the Airwallex client,
executor and fake). `infra/` bundles the handlers into Lambda functions, every kit runs its
tests through `local_runner`, and `awx_agent`'s tool Lambdas reuse `engine`, `codec`, the kit
store and the tenant resolver.

## Modules

| Module | What it holds |
|---|---|
| `engine` | The phases of one kit workflow run (start, binding, recheck, fan_out, build, result, crashed, completed, finality_check, postcondition, abandon, audit), the run payload codec, read retries and read scope checks. Runner rules 0 to 11 are defined in the `awx_common.workflow` module docstring. |
| `local_runner` | `run_kit_workflow`: the same phases in-process, in the state machine's order, with `adapter_core.dispatch` as the adapter. `RunRegistry` answers `run_status` locally. |
| `adapter_core` | What the adapter does with one operation in each mode (`send`, `resend`, `advance`, `complete`), shared by the adapter Lambda and `local_runner`. |
| `codec` | Rebuilds `awx_common` dataclasses from transport payloads; carries an Airwallex error's code, `http_status` and details across the reader Lambda. |
| `store` | `DynamoDbOperationStore`: `awx_spine`'s `OperationStore` on DynamoDB. |
| `kit_store` | `DynamoDbKitStore`: `awx_common.store.KitStore` on the ledger table, one partition per kit and tenant. |
| `repos` | `ProposalRepo`, `DecisionRepo`, `DynamoDbTenantResolver` and `AuditRepo`. |
| `credentials` | `SecretsManagerCredentials`: Airwallex sandbox credentials from Secrets Manager. |
| `evidence` | `S3EvidenceStore`: evidence blobs under `evidence/<kit>/<tenant_id>/` in the artifacts bucket. |
| `deploy` | The `awx-deploy` CLI. |
| `handlers.kit_step` | Runs one engine phase per state machine task; `LambdaEnv` is the deployed `engine.Env`. |
| `handlers.adapter` | The mutation adapter: the only code path that sends an Airwallex write. |
| `handlers.reader` | Airwallex GETs only, one reader per kit, server-resolved tenant. |
| `handlers.approval` | The approval HTTP API (list, read and decide proposals). |
| `handlers.pre_token` | Cognito Pre Token Generation trigger that adds the `tenant_id` claim to the access token. |
| `handlers._common` | Environment lookup, cached boto3 clients, one-line JSON logging. |

## Key behaviours

One engine, two runners. Every Lambda task in the state machine calls one `engine` phase
through `handlers.kit_step`, and `local_runner` calls the same functions in the same order, so
a kit tested locally exercises the code that runs in AWS. `tests/test_asl_equivalence.py`
interprets the synthesised state machine definition against the real handlers and checks it
reaches the same outcome as `local_runner`.

Only the adapter writes. `handlers.adapter.authorise` re-checks, from the tables rather than
its input, that the proposal is intact and approved (and its fan-out parent too), that the
operation carries the proposal's kit and intent hash, and that it passes
`tenancy.check_tenant_scope` for the proposal's tenant. It then runs one attempt per call
(`adapter_core.MAX_ATTEMPTS_PER_CALL`), so a Lambda timeout cannot leave several sends in
flight. The body goes out exactly as the kit built it.

The runner owns every resend (rule 9). After an unknown outcome or an adapter crash,
`engine._after_unknown` runs the step's reconcile first, resends only an endpoint that
deduplicates on request_id (`engine.dedupes`), and stops at once on a code a resend cannot
change (`engine.FINAL_UNKNOWN_CODES`). Resends back off 5, 10 and 20 seconds
(`MAX_RESENDS` is 3); after a crash or `in_progress_elsewhere` a resend waits out the
executor's 180 second attempt lease plus 15 seconds (`LEASE_RESEND_WAIT_S`), so the executor
reconciles the earlier attempt instead of answering `in_progress_elsewhere`.

A reconciled object is checked like a read-back. `engine._settle_found` treats an object that
does not echo the operation's amount, currency or counterparty as OUTCOME_UNKNOWN
`request_id_reused` (it may be an earlier body of this operation, so money may have moved),
never a definitive FAILED; an object already in a terminal failure state is FAILED.

Every dispatched operation reaches `StepContext.prior`. `engine.error` records a pending
operation's established result, or OUTCOME_UNKNOWN. Only a refusal at an operation's first
dispatch leaves it absent; a `RequestIdReuse` is recorded as OUTCOME_UNKNOWN with the stored
record's status in `body["last_error"]`. When a send fails after earlier sends of the same
step left, those sends still run the finality loop before the run fails.

Finality reads tolerate transient failures. `engine.transient_read_failure` treats a 429, a
5xx, a read timeout, and a NotFound within 30 seconds of the loop's start
(`NOT_FOUND_GRACE_S`, list lag) as "not final yet"; any other failure fails the run closed
and keeps what the poll already found.

One retry layer on reads. `engine.retrying` retries a transient read up to 3 times with 1, 2
and 4 second backoff, or the error's Retry-After (capped at 30 seconds), only while the wait
fits the caller's budget; in a Lambda that is the remaining time minus one reader call
(`LambdaEnv.MARGIN_S`). `engine.read_once` asks `awx_spine`'s client for a single attempt.

Reads are scoped by the kit, never the caller. Each kit has its own reader Lambda
(`AWX_READER_KIT`) holding only that kit's allowlists; a request names a canonical
`/api/v1/...` path that must fully match a pattern (`engine.path_allowed`). The read mode is
derived from the resolved tenant and the `x-on-behalf-of` header, and a platform-tenant read
uses the kit's platform allowlist when it declares one (`engine.own_account_patterns`), so
naming the platform tenant never widens a connected-account kit's reach.

Tenants rows are kit-bound. `DynamoDbTenantResolver.resolve` accepts a row only for the
calling kit; a row with no `connected_account_id` must have role `operator`, and a malformed
account is refused on every role. The engine (`check_tenant_kit`) and the kit step's Start
refuse a tenant resolved in another kit.

Approvals bind to the hash and the approver tenant. The approval API authorises on the
access token's `sub`, `tenant_id` claim and `cognito:groups`, needs the intent hash the
approver was shown, refuses a non-binding decision before storing it, and sends the task token
once the proposal is APPROVED or REJECTED. Each proposal row records the `approver_tenant`
from `awx_common.workflow.approver_tenant`; only that tenant may decide, and a kit 5 to 8 row
without one cannot be decided.

`run_status` reports terminal only when a run can send nothing more. `LambdaEnv.run_status`
describes the root execution `awx-<kit>-<action>:<proposal_id>`; a stopped run reads
`RUN_RUNNING` until the adapter timeout plus a margin has passed (`settled_status`). After an
abnormal end it also waits the child machine timeout, and so does a SUCCEEDED end of an action
with children unless the run's outcome is SUCCEEDED with no lost child, or every lost child
reads terminal.

The operation store records before the call. `DynamoDbOperationStore` writes each request_id
with a conditional put before the send, sets `may_have_landed` in the same update that records
an OUTCOME_UNKNOWN (never cleared), keeps the first definitive result, and has no time to live
(TTL), because Airwallex deduplicates a request_id for only about 7 days.

Pure kit callables cannot write. `validate`, `policy`, `fan_out` and `child_tenant` see a
read-only store (`engine._ReadOnlyStore`, rule 0).

## awx-deploy

`awx-deploy` loads deployed state that a kit cannot set up itself: kit-bound tenants rows
(`tenants put`), one kit operator command (`run`), a per-kit deploy file (`plan`, `apply`),
and a schedule-driven action (`trigger`, origin SCHEDULE). It uses operator AWS credentials
and never moves money. The runbook is in [docs/deployment.md](../../docs/deployment.md).

## Tests

Shared fixtures live in `tests/conftest.py` (`aws`, `testkit` and `machines`),
`tests/aws_harness.py` (moto tables and bucket, the handlers wired together in-process) and
`tests/toy.py` (a toy kit and a toy Airwallex port with fault injection).

| Topic | Files |
|---|---|
| Engine and local runner | `test_local_runner.py`, `test_engine_payload.py`, `test_asl_equivalence.py` |
| Lambda handlers | `test_handlers.py` |
| Approvals | `test_approver_tenant.py` |
| Stores and credentials | `test_store.py`, `test_kit_store.py`, `test_evidence.py`, `test_credentials.py` |
| Results on failure | `test_prior_on_failure.py`, `test_request_id_reuse.py` |
| Resends and unknown outcomes | `test_unknown_outcomes.py`, `test_attempt_lease.py` |
| Finality | `test_finality_reads.py`, `test_finality_kit_errors.py` |
| Reads | `test_read_retries.py`, `test_reader_path.py` |
| Clocks | `test_fake_clock.py` |
| run_status and fan-out | `test_run_status.py`, `test_run_status_lost_children.py`, `test_fan_out_root_run.py` |
| Tenancy | `test_kit_bound_tenants.py` |
| awx-deploy | `test_deploy.py` |

Unmarked tests run offline against moto and the toy port. Tests marked `synth` synthesise the
CDK app (the `machines` fixture builds the state machine definitions the interpreter runs) and
are slower. No test in this package calls the Airwallex sandbox or deployed AWS resources.

```
uv run --all-packages pytest packages/awx_spine_aws/tests -m "not sandbox and not synth and not live"
uv run --all-packages pytest packages/awx_spine_aws/tests -m synth
```
