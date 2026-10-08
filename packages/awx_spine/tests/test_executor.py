from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import pytest
from awx_common import Operation, OperationResult, OperationStatus, ReadRequest
from awx_common.errors import NotFound, TransientServerError
from awx_common.evidence import FILES_UPLOAD_OP, FILES_UPLOAD_PATH, InMemoryEvidenceStore
from awx_common.workflow import ReconcileUndetermined
from awx_spine import FakeAirwallex, Fault
from awx_spine.executor import RECOVERED_FROM, MutationExecutor
from awx_spine.store import InMemoryOperationStore, RequestIdReuse, UnknownRequestId, body_hash
from awx_spine.testing import after_lease, make_op, transfer_op

OK = OperationStatus.SUCCEEDED
FAILED = OperationStatus.FAILED
UNKNOWN = OperationStatus.OUTCOME_UNKNOWN


def res(op: Operation, status: OperationStatus, **kw: Any) -> OperationResult:
    return OperationResult(request_id=op.request_id, status=status, **kw)


@dataclass
class StubPort:
    """Scripted execute results; reads come from a callable. Records every call in order."""

    results: list[Callable[[Operation], OperationResult]] = field(default_factory=list)
    reads: Callable[[ReadRequest], Mapping[str, Any]] = lambda req: {"items": []}
    calls: list[tuple[str, Any]] = field(default_factory=list)
    store: InMemoryOperationStore | None = None
    uploads: list[tuple[bytes, str]] = field(default_factory=list)

    def execute(self, op: Operation) -> OperationResult:
        if self.store is not None:  # record-before-call: the record must already exist
            rec = self.store.get(op.request_id)
            assert rec is not None
            assert rec.attempts >= 1
        self.calls.append(("execute", op))
        return self.results.pop(0)(op)

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        self.calls.append(("read", req))
        return self.reads(req)

    def upload(
        self,
        data: bytes,
        *,
        file_name: str,
        content_type: str,
        notes: str | None = None,
        on_behalf_of: str | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append(("upload", file_name))
        self.uploads.append((data, content_type))
        return {"file_id": "file_1", "filename": file_name, "object_type": "file"}

    def executes(self) -> list[Operation]:
        return [c[1] for c in self.calls if c[0] == "execute"]


def executor(port: StubPort, **kw: Any) -> tuple[MutationExecutor, InMemoryOperationStore]:
    store = InMemoryOperationStore()
    port.store = store
    return MutationExecutor(port, store, sleep=lambda s: None, **kw), store


def created(op: Operation) -> OperationResult:
    return res(op, OK, http_status=201, provider_id="t1", provider_status="SCHEDULED")


# ---- store --------------------------------------------------------------------------------------


def test_store_begin_is_idempotent_and_refuses_changed_body():
    store = InMemoryOperationStore()
    op = transfer_op()
    first = store.begin(op)
    assert first.status == "PENDING"
    assert first.attempts == 0
    assert first.result is None
    assert store.begin(op) == first
    with pytest.raises(RequestIdReuse):
        store.begin(replace(op, body={**op.body, "transfer_amount": Decimal("11.00")}))
    with pytest.raises(RequestIdReuse):
        store.begin(replace(op, on_behalf_of="acct_other"))
    with pytest.raises(UnknownRequestId):
        store.record_attempt("nope")
    assert body_hash(op) == first.body_hash


def test_store_complete_requires_matching_request_id():
    store = InMemoryOperationStore()
    op = transfer_op()
    store.begin(op)
    with pytest.raises(ValueError, match="different request_id"):
        store.complete(op.request_id, OperationResult(request_id="other", status=OK))


# ---- executor -----------------------------------------------------------------------------------


def test_record_is_written_before_the_call_and_completed_after():
    port = StubPort([created])
    ex, store = executor(port)
    op = transfer_op()
    result = ex.run(op)
    assert result.status is OK
    rec = store.get(op.request_id)
    assert rec.status == "SUCCEEDED"
    assert rec.attempts == 1
    assert rec.result == result


def test_rerun_of_completed_operation_never_calls_the_provider():
    port = StubPort([created])
    ex, _ = executor(port)
    op = transfer_op()
    ex.run(op)
    assert ex.run(op).provider_id == "t1"
    assert len(port.executes()) == 1


def test_changed_body_under_same_request_id_raises_before_any_call():
    port = StubPort([created])
    ex, _ = executor(port)
    op = transfer_op()
    ex.run(op)
    with pytest.raises(RequestIdReuse):
        ex.run(replace(op, body={**op.body, "transfer_amount": Decimal("99.00")}))
    assert len(port.executes()) == 1


def test_retry_reuses_request_id_and_body_only_for_retryable_errors():
    def rate_limited(op):
        return res(op, FAILED, http_status=429, error_type="RateLimited")

    port = StubPort([rate_limited, created])
    ex, store = executor(port)
    op = transfer_op()
    assert ex.run(op).status is OK
    a, b = port.executes()
    assert a == b
    assert a.request_id == b.request_id
    assert a.body == b.body
    assert store.get(op.request_id).attempts == 2


def test_non_retryable_failure_is_not_retried_or_resent_later():
    def nsf(op):
        return res(
            op,
            FAILED,
            http_status=400,
            error_type="InsufficientFunds",
            error_code="balance_insufficient",
        )

    port = StubPort([nsf])
    ex, _ = executor(port)
    op = transfer_op()
    assert ex.run(op).error_type == "InsufficientFunds"
    assert ex.run(op).error_type == "InsufficientFunds"
    assert len(port.executes()) == 1


def landed(op: Operation, **extra: Any) -> dict[str, Any]:
    """The object the provider lists for op: it echoes the body, as the sandbox does."""
    return {**op.body, "id": "t1", "status": "PROCESSING", **extra}


def unknown(op: Operation) -> OperationResult:
    return res(op, UNKNOWN, error_type="OutcomeUnknown")


def test_unknown_outcome_reads_back_before_resending_and_finds_original():
    op = transfer_op()
    port = StubPort([unknown], reads=lambda req: {"items": [landed(op)]})
    ex, store = executor(port)
    result = ex.run(op)
    assert result.status is OK
    assert result.provider_id == "t1"
    assert result.body[RECOVERED_FROM] == "read_back"
    assert [c[0] for c in port.calls] == ["execute", "read"]
    read = port.calls[1][1]
    assert read.path == "/api/v1/transfers"
    assert read.query == {"request_id": op.request_id}
    assert store.get(op.request_id).status == "SUCCEEDED"


def test_read_back_waits_before_reading():
    op = transfer_op()
    sleeps: list[float] = []
    port = StubPort([unknown], reads=lambda req: {"items": [landed(op)]})
    store = InMemoryOperationStore()
    MutationExecutor(port, store, sleep=sleeps.append).run(op)
    assert sleeps
    assert sleeps[0] > 0


def test_unknown_outcome_not_found_resends_same_request_id_on_dedup_endpoint():
    port = StubPort([unknown, created])
    ex, _ = executor(port)
    op = transfer_op()
    assert ex.run(op).status is OK
    assert [c[0] for c in port.calls] == ["execute", "read", "execute"]
    assert port.executes()[0] == port.executes()[1]


def test_read_back_inherits_on_behalf_of():
    op = transfer_op(on_behalf_of="acct_x")
    port = StubPort([unknown, created])
    ex, _ = executor(port)
    ex.run(op)
    assert port.calls[1][1].on_behalf_of == "acct_x"


def test_unknown_exhausted_stays_unknown_and_next_run_reads_first():
    op = transfer_op()
    port = StubPort([unknown] * 3)
    ex, store = executor(port)
    assert ex.run(op).status is UNKNOWN
    assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"
    port.reads = lambda req: {"items": [landed(op, id="t9", status="PAID")]}
    later = ex.run(op)
    assert later.status is OK
    assert later.provider_id == "t9"
    assert len(port.executes()) == 3  # the later run never resent


def test_reconcile_hook_replaces_generic_read_back():
    seen = []
    op = make_op("k2", "issuing.cards.create", "/api/v1/issuing/cards/create", {"x": 1}, "card:1")

    def reconcile(o):
        seen.append(o.request_id)
        return {"card_id": "card_1", "card_status": "ACTIVE"}

    port = StubPort([unknown])
    ex, _ = executor(port)
    result = ex.run(op, reconcile=reconcile)
    assert result.status is OK
    assert result.provider_id == "card_1"
    assert result.body[RECOVERED_FROM] == "reconcile"
    assert seen == [op.request_id]
    assert [c[0] for c in port.calls] == ["execute"]  # no generic read


def test_reconcile_undetermined_stops_without_resend():
    op = transfer_op()

    def reconcile(o):
        raise ReconcileUndetermined("ledger unavailable")

    port = StubPort([unknown, created])
    ex, _ = executor(port)
    assert ex.run(op, reconcile=reconcile).status is UNKNOWN
    assert len(port.executes()) == 1


def test_reconcile_not_landed_resends_only_dedup_endpoints():
    op = transfer_op()
    port = StubPort([unknown, created])
    ex, _ = executor(port)
    assert ex.run(op, reconcile=lambda o: None).status is OK
    assert len(port.executes()) == 2
    sim = make_op(
        "k2",
        "issuing.simulate_authorization",
        "/api/v1/simulation/issuing/create",
        {"card_id": "c"},
        "auth:1",
        with_request_id=False,
    )
    port = StubPort([unknown, created])
    ex, _ = executor(port)
    assert ex.run(sim, reconcile=lambda o: None).status is UNKNOWN
    assert len(port.executes()) == 1


def test_read_back_errors_keep_outcome_unknown_without_resend():
    def boom(req):
        raise TransientServerError("down")

    port = StubPort([unknown, created], reads=boom)
    ex, _ = executor(port)
    assert ex.run(transfer_op()).status is UNKNOWN
    assert len(port.executes()) == 1  # could not show it did not land: no resend


@pytest.mark.parametrize(
    ("name", "path", "body"),
    [
        ("issuing.simulate_authorization", "/api/v1/simulation/issuing/create", {"card_id": "c"}),
        ("deposits.simulate", "/api/v1/simulation/deposit/create", {"amount": Decimal("1")}),
        (
            "transfers.simulate_transition",
            "/api/v1/simulation/transfers/t/transition",
            {"next_status": "SENT"},
        ),
        ("beneficiaries.create", "/api/v1/beneficiaries/create", {"nickname": "poc"}),
        ("fx.quotes.create", "/api/v1/fx/quotes/create", {"validity": "MIN_1"}),
        ("accounts.create", "/api/v1/accounts/create", {"account_details": {}}),
        ("made.up", "/api/v1/made/up", {}),
    ],
)
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_endpoints_that_ignore_request_id_are_never_resent(name, path, body, failure):
    op = make_op("k2", name, path, body, f"nd:{name}:{failure}", with_request_id=False)

    def fail(o):
        code = 500 if failure == "server_error" else None
        return res(o, UNKNOWN, http_status=code, error_type="OutcomeUnknown")

    port = StubPort([fail, created, created])
    ex, store = executor(port)
    assert ex.run(op).status is UNKNOWN
    assert len(port.executes()) == 1
    assert ex.run(op).status is UNKNOWN  # a later run does not resend either
    assert len(port.executes()) == 1
    assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"


def test_fake_card_authorisation_and_deposit_are_not_doubled():
    fake = FakeAirwallex(balances={"USD": __import__("awx_common").Money.of("100", "USD")})
    ex = MutationExecutor(fake, InMemoryOperationStore(), sleep=lambda s: None)
    ch = ex.run(
        make_op(
            "k2",
            "issuing.cardholders.create",
            "/api/v1/issuing/cardholders/create",
            {"type": "DELEGATE", "email": "poc-x@example.com"},
            "ch",
            with_request_id=False,
        )
    )
    card = ex.run(
        make_op(
            "k2",
            "issuing.cards.create",
            "/api/v1/issuing/cards/create",
            {
                "cardholder_id": ch.provider_id,
                "created_by": "poc",
                "form_factor": "VIRTUAL",
                "is_personalized": False,
                "program": {"purpose": "COMMERCIAL"},
                "authorization_controls": {
                    "allowed_transaction_count": "MULTIPLE",
                    "transaction_limits": {"currency": "USD", "limits": []},
                },
            },
            "card",
        )
    )
    for fault in (Fault.TIMEOUT_AFTER_SUBMIT, Fault.SERVER_ERROR):
        fake.inject("issuing.simulate_authorization", fault, applied=fault is Fault.SERVER_ERROR)
        op = make_op(
            "k2",
            "issuing.simulate_authorization",
            "/api/v1/simulation/issuing/create",
            {
                "card_id": card.provider_id,
                "transaction_amount": Decimal("1.00"),
                "transaction_currency": "USD",
            },
            f"auth:{fault}",
            with_request_id=False,
        )
        assert ex.run(op).status is UNKNOWN
    assert len(fake.objects("card_transactions")) == 2  # one per operation, none doubled


def test_pending_record_with_an_attempt_reads_back_first():
    op = transfer_op()
    port = StubPort([created], reads=lambda req: {"items": [landed(op)]})
    ex, store = executor(port, clock=after_lease)
    store.begin(op)
    store.record_attempt(op.request_id)  # the process died after sending
    result = ex.run(op)
    assert result.status is OK
    assert result.body[RECOVERED_FROM] == "read_back"
    assert port.executes() == []


def test_pending_record_on_non_dedup_endpoint_is_unknown_not_resent():
    op = make_op(
        "k2",
        "issuing.simulate_authorization",
        "/api/v1/simulation/issuing/create",
        {"card_id": "c"},
        "pend",
        with_request_id=False,
    )
    port = StubPort([created])
    ex, store = executor(port)
    store.begin(op)
    store.record_attempt(op.request_id)
    assert ex.run(op).status is UNKNOWN
    assert port.executes() == []


def test_read_back_mismatch_fails_closed():
    op = transfer_op("10.00")
    other = landed(op, transfer_amount=Decimal("11.00"))
    port = StubPort([unknown], reads=lambda req: {"items": [other]})
    ex, _ = executor(port)
    result = ex.run(op)
    assert result.status is UNKNOWN  # a mismatch: never SUCCEEDED, never FAILED
    assert result.error_code == "request_id_reused"
    assert result.body["last_error"]["error_type"] == "StateConflict"
    assert len(port.executes()) == 1


def test_read_back_missing_echoed_field_fails_closed_and_decimal_equality_holds():
    op = transfer_op("10.00")
    partial = {k: v for k, v in landed(op).items() if k != "beneficiary_id"}
    port = StubPort([unknown], reads=lambda req: {"items": [partial]})
    ex, _ = executor(port)
    assert ex.run(op).error_code == "request_id_reused"
    same = landed(op, transfer_amount=Decimal("10.0"))  # the provider writes 10.0
    port = StubPort([unknown], reads=lambda req: {"items": [same]})
    ex, _ = executor(port)
    assert ex.run(op).status is OK


def test_duplicate_against_a_different_payment_on_the_fake_fails_closed():
    """A fresh store (second deployment) reuses a business key with an edited amount."""
    from awx_common import Money

    fake = FakeAirwallex(balances={"USD": Money.of("100", "USD")})
    acct = fake.add_connected_account("poc-k8", {})
    body = {
        "amount": Decimal("10.00"),
        "currency": "USD",
        "destination": acct,
        "reason": "goods_purchased",
        "reference": "poc-k8",
    }
    first = make_op(
        "k8",
        "connected_account_transfers.create",
        "/api/v1/connected_account_transfers/create",
        body,
        "payout:1",
    )
    assert MutationExecutor(fake, InMemoryOperationStore()).run(first).status is OK
    edited = make_op("k8", first.name, first.path, {**body, "amount": Decimal("12.00")}, "payout:1")
    result = MutationExecutor(fake, InMemoryOperationStore(), sleep=lambda s: None).run(edited)
    assert result.status is UNKNOWN  # it may be our own earlier body
    assert result.error_code == "request_id_reused"
    assert len(fake.objects("connected_account_transfers")) == 1


def test_duplicate_request_resolves_to_original_with_bounded_read_back_retry():
    op = transfer_op()
    reads = iter([{"items": []}, {"items": []}, {"items": [landed(op)]}])

    def dup(o):
        return res(
            o,
            FAILED,
            http_status=400,
            error_type="DuplicateRequest",
            error_code="duplicate_request_id",
            provider_id="t1",
        )

    port = StubPort([dup], reads=lambda req: next(reads))
    ex, _ = executor(port)
    result = ex.run(op)
    assert result.status is OK
    assert result.provider_id == "t1"
    assert result.body[RECOVERED_FROM] == "duplicate_request"
    assert len(port.executes()) == 1


def test_duplicate_without_read_back_is_unknown_even_with_an_id():
    op = make_op("k5", "global_accounts.create", "/api/v1/global_accounts/create", {}, "ga:1")
    dup = lambda o: res(o, FAILED, error_type="DuplicateRequest", provider_id="ga_1")  # noqa: E731
    port = StubPort([dup])
    ex, _ = executor(port)
    result = ex.run(op)
    assert result.status is UNKNOWN
    assert result.provider_id == "ga_1"
    assert len(port.executes()) == 1


def test_duplicate_that_cannot_be_read_back_is_outcome_unknown_not_resent():
    op = make_op("k2", "issuing.cards.create", "/api/v1/issuing/cards/create", {}, "card:2")

    def dup(o):
        return res(
            o,
            FAILED,
            error_type="DuplicateRequest",
            error_code="invalid_argument",
        )

    port = StubPort([dup])
    ex, _ = executor(port)
    assert ex.run(op).status is UNKNOWN
    assert len(port.executes()) == 1


def test_cardholder_email_collision_resolves_by_email_read_back():
    fake = FakeAirwallex()
    op = make_op(
        "k2",
        "issuing.cardholders.create",
        "/api/v1/issuing/cardholders/create",
        {"type": "DELEGATE", "email": "poc-k2-dup@example.com"},
        "chd",
        with_request_id=False,
    )
    fake.inject(op.name, Fault.DUPLICATE_DELIVERY)
    result = MutationExecutor(fake, InMemoryOperationStore(), sleep=lambda s: None).run(op)
    assert result.status is OK
    assert result.provider_id == fake.objects("cardholders")[0]["cardholder_id"]


def test_files_upload_verifies_evidence_and_uploads():
    store = InMemoryEvidenceStore()
    ref = store.put("ev1", b"\xff\xd8jpegbytes", file_name="ev1.jpg", content_type="image/jpeg")
    op = make_op(
        "k4",
        FILES_UPLOAD_OP,
        FILES_UPLOAD_PATH,
        {"evidence_ref": ref.to_body(), "notes": "poc-k4-ev1"},
        "ev:1",
        with_request_id=False,
    )
    port = StubPort()
    ex, _ = executor(port, evidence_store=store)
    result = ex.run(op)
    assert result.status is OK
    assert result.provider_id == "file_1"
    assert port.uploads == [(b"\xff\xd8jpegbytes", "image/jpeg")]


def test_files_upload_unknown_outcome_is_resent_because_duplicates_are_inert():
    store = InMemoryEvidenceStore()
    ref = store.put("ev1", b"%PDF x", file_name="ev1.pdf", content_type="application/pdf")
    fake = FakeAirwallex()
    fake.inject(FILES_UPLOAD_OP, Fault.TIMEOUT_AFTER_SUBMIT)
    op = make_op(
        "k4",
        FILES_UPLOAD_OP,
        FILES_UPLOAD_PATH,
        {"evidence_ref": ref.to_body()},
        "ev:resend",
        with_request_id=False,
    )
    ex = MutationExecutor(
        fake, InMemoryOperationStore(), sleep=lambda s: None, evidence_store=store
    )
    assert ex.run(op).status is OK
    assert len(fake.objects("files")) == 2


@pytest.mark.parametrize("problem", ["no_store", "missing", "tampered", "bad_name"])
def test_files_upload_refuses_bad_evidence_without_retry(problem):
    store = InMemoryEvidenceStore()
    ref = store.put("ev1", b"abc", file_name="ev1.pdf", content_type="application/pdf")
    if problem == "missing":
        ref = replace(ref, evidence_id="ev2")
    if problem == "tampered":
        store.put("ev1", b"abd", file_name="ev1.pdf", content_type="application/pdf")
    if problem == "bad_name":
        ref = store.put("ev1", b"abc", file_name='ev"1.pdf', content_type="application/pdf")
    op = make_op(
        "k4",
        FILES_UPLOAD_OP,
        FILES_UPLOAD_PATH,
        {"evidence_ref": ref.to_body()},
        f"ev:{problem}",
        with_request_id=False,
    )
    port = StubPort()
    if problem == "bad_name":

        def upload(data, *, file_name, content_type, notes=None, on_behalf_of=None):
            raise ValueError("file_name must be a bare file name")

        port.upload = upload  # type: ignore[method-assign]
    ex, store_ = executor(port, evidence_store=None if problem == "no_store" else store)
    result = ex.run(op)
    assert result.status is FAILED
    assert result.error_type == "ValidationFailed"
    assert port.uploads == []
    assert ex.run(op).status is FAILED
    assert port.uploads == []
    assert store_.get(op.request_id).status == "FAILED"


def test_read_errors_other_than_retryable_keep_it_unknown():
    def nf(req):
        raise NotFound("gone")

    port = StubPort([unknown, created], reads=nf)
    ex, _ = executor(port)
    assert ex.run(transfer_op()).status is UNKNOWN
