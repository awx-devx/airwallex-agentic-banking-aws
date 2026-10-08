"""Uncertain sends: once a send may have landed, a refused resend is never definitive.

Also covers two objects under one request_id, the not-sent path, source_amount on read-back,
and the FakeAirwallex guards these scenarios rely on (typed paging errors, no simulation on a
hidden object).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from awx_common import Money, Operation, OperationResult, OperationStatus, ReadRequest
from awx_common.errors import AwxError, TransientServerError, ValidationFailed
from awx_common.evidence import FILES_UPLOAD_OP, FILES_UPLOAD_PATH, InMemoryEvidenceStore
from awx_spine import FakeAirwallex, Fault, InMemoryOperationStore, MutationExecutor
from awx_spine.issuing import find_card_transaction
from awx_spine.testing import after_lease, make_op, transfer_op

OK = OperationStatus.SUCCEEDED
FAILED = OperationStatus.FAILED
UNKNOWN = OperationStatus.OUTCOME_UNKNOWN
USD = "USD"
LAG = 600


def usd(x: str) -> Money:
    return Money.of(x, USD)


def setup(balance: str = "100.00") -> tuple[FakeAirwallex, Operation]:
    fake = FakeAirwallex(balances={USD: usd(balance)})
    ben = fake.execute(
        make_op(
            "k3", "beneficiaries.create", "/api/v1/beneficiaries/create", {"beneficiary": {}}, "ben"
        )
    ).provider_id
    op = make_op(
        "k3",
        "transfers.create",
        "/api/v1/transfers/create",
        {
            "beneficiary_id": ben,
            "transfer_amount": Decimal("60.00"),
            "transfer_currency": USD,
            "transfer_method": "LOCAL",
            "reference": "poc-r2s",
        },
        "supplier-invoice:1",
    )
    return fake, op


def executor(fake: FakeAirwallex, store: InMemoryOperationStore, attempts: int = 3, **kw):
    return MutationExecutor(
        fake, store, sleep=fake.advance, backoff_s=1, max_attempts=attempts, **kw
    )


def posts(fake: FakeAirwallex, op: Operation) -> list[int]:
    return [i for i, r in enumerate(fake.requests) if r[0] == "POST" and r[1] == op.path]


def gets(fake: FakeAirwallex) -> list[int]:
    return [i for i, r in enumerate(fake.requests) if r[0] == "GET"]


# ---- a refused resend after an invisible landing ----------------------------------------------


def test_refused_resend_after_an_invisible_landing_stays_unknown_then_resolves():
    fake, op = setup()
    fake.inject(op.name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=LAG)  # lands, invisible
    fake.inject(op.name, Fault.INSUFFICIENT_FUNDS)  # the resend is refused
    store = InMemoryOperationStore()
    result = executor(fake, store).run(op)
    assert result.status is UNKNOWN
    assert result.body["refused"] == {
        "error_type": "InsufficientFunds",
        "error_code": "balance_insufficient",
        "http_status": 400,
        "error_message": result.body["refused"]["error_message"],
    }
    assert result.error_code is None  # the refusal stays in the body
    assert result.http_status is None
    rec = store.get(op.request_id)
    assert rec.status == "OUTCOME_UNKNOWN"
    assert rec.may_have_landed
    assert len(posts(fake, op)) == 2  # the first send and one resend, nothing more
    fake.advance(LAG)
    later = executor(fake, store).run(op)
    assert later.status is OK
    assert later.provider_id == fake.objects("transfers")[0]["id"]
    assert len(fake.objects("transfers")) == 1
    assert len(posts(fake, op)) == 2
    assert store.get(op.request_id).may_have_landed  # never cleared


def test_unknown_then_429_then_a_later_run_reads_back_before_its_refused_send():
    fake, op = setup()
    store = InMemoryOperationStore()
    fake.inject(op.name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=LAG)
    assert executor(fake, store, 1).run(op).status is UNKNOWN  # call 1
    fake.inject(op.name, Fault.RATE_LIMIT)
    second = executor(fake, store, 1).run(op)  # call 2: the resend gets a 429
    assert second.status is UNKNOWN  # never FAILED while uncertain
    assert second.body["last_error"]["error_type"] == "RateLimited"
    assert store.get(op.request_id).may_have_landed
    fake.inject(op.name, Fault.INSUFFICIENT_FUNDS)
    mark = len(fake.requests)
    third = executor(fake, store, 1).run(op)  # call 3
    tail = fake.requests[mark:]
    assert [r[0] for r in tail][:2] == ["GET", "POST"]  # read back BEFORE sending
    assert third.status is UNKNOWN
    assert third.body["refused"]["error_code"] == "balance_insufficient"
    assert len(fake.objects("transfers")) == 1


class SpyStore(InMemoryOperationStore):
    def __init__(self, log: list[tuple[str, str]]) -> None:
        super().__init__()
        self.log = log

    def complete(self, request_id: str, result: OperationResult):
        self.log.append(("complete", str(result.status)))
        return super().complete(request_id, result)


class SpyPort:
    def __init__(self, fake: FakeAirwallex, log: list[tuple[str, str]]) -> None:
        self.fake, self.log = fake, log

    def execute(self, op: Operation) -> OperationResult:
        self.log.append(("send", op.name))
        return self.fake.execute(op)

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        self.log.append(("read", req.path))
        return self.fake.read(req)


def test_crash_after_record_attempt_persists_unknown_before_reconciling():
    fake, op = setup()
    fake.inject(op.name, Fault.LIST_LAG, visible_after_s=LAG)
    assert fake.execute(op).status is OK  # the send that left before the crash
    log: list[tuple[str, str]] = []
    store = SpyStore(log)
    store.begin(op)
    store.record_attempt(op.request_id)  # ...and the process died before complete
    fake.inject(op.name, Fault.INSUFFICIENT_FUNDS)
    result = MutationExecutor(SpyPort(fake, log), store, sleep=fake.advance, clock=after_lease).run(
        op
    )
    assert log[0] == ("complete", "OUTCOME_UNKNOWN")
    assert log[1][0] == "read"
    assert result.status is UNKNOWN
    assert result.body["refused"]["error_type"] == "InsufficientFunds"
    assert store.get(op.request_id).may_have_landed
    assert len(fake.objects("transfers")) == 1


# ---- controls ---------------------------------------------------------------------------------


def test_429_then_refusal_with_no_unknown_before_is_failed():
    fake, op = setup()
    fake.inject(op.name, Fault.RATE_LIMIT)
    fake.inject(op.name, Fault.INSUFFICIENT_FUNDS)
    store = InMemoryOperationStore()
    result = executor(fake, store).run(op)
    assert result.status is FAILED
    assert result.error_type == "InsufficientFunds"
    assert not store.get(op.request_id).may_have_landed


def test_refusal_on_the_first_send_is_failed():
    fake, op = setup(balance="10.00")
    store = InMemoryOperationStore()
    result = executor(fake, store).run(op)
    assert (result.status, result.error_code) == (FAILED, "balance_insufficient")
    assert not store.get(op.request_id).may_have_landed


def test_duplicate_on_the_resend_still_resolves_through_the_read_back():
    fake, op = setup()
    fake.inject(op.name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3)
    result = executor(fake, InMemoryOperationStore()).run(op)
    assert result.status is OK
    assert result.body["_awx_recovered_from"] == "duplicate_request"
    assert len(fake.objects("transfers")) == 1


class UploadPort:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes, self.calls = outcomes, 0

    def execute(self, op):  # pragma: no cover - not used
        raise AssertionError

    def read(self, req):  # pragma: no cover - not used
        raise AssertionError

    def upload(self, data, *, file_name, content_type, notes=None, on_behalf_of=None):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_files_upload_is_unchanged_a_refusal_after_unknown_stays_failed():
    ev = InMemoryEvidenceStore()
    ref = ev.put("ev1", b"%PDF x", file_name="ev1.pdf", content_type="application/pdf")
    op = make_op(
        "k4",
        FILES_UPLOAD_OP,
        FILES_UPLOAD_PATH,
        {"evidence_ref": ref.to_body()},
        "ev",
        with_request_id=False,
    )
    from awx_common.errors import OutcomeUnknown

    port = UploadPort([OutcomeUnknown("timeout"), ValidationFailed("bad file", code="x")])
    result = MutationExecutor(
        port, InMemoryOperationStore(), sleep=lambda s: None, evidence_store=ev
    ).run(op)
    assert result.status is FAILED
    assert result.error_type == "ValidationFailed"
    assert port.calls == 2


# ---- two objects under one request_id ---------------------------------------------------------


def test_two_objects_under_one_request_id_is_unknown_naming_both():
    fake, op = setup(balance="500.00")
    fake.execute(op)
    fake.advance(7 * 86400 + 1)  # past the deduplication window
    fake.execute(op)  # a second transfer under the same request_id
    ids = [t["id"] for t in fake.objects("transfers")]
    assert len(ids) == 2
    result = executor(fake, InMemoryOperationStore()).run(op)  # fresh store: duplicate
    assert result.status is UNKNOWN
    assert result.error_code == "multiple_objects"
    assert sorted(result.body["provider_ids"]) == sorted(ids)
    for i in ids:
        assert i in result.error_message
    store = InMemoryOperationStore()  # and on resume of a crashed record
    store.begin(op)
    store.record_attempt(op.request_id)
    resumed = executor(fake, store, clock=after_lease).run(op)
    assert resumed.status is UNKNOWN
    assert resumed.error_code == "multiple_objects"
    assert len(fake.objects("transfers")) == 2


# ---- the not-sent path and source_amount ------------------------------------------------------


class Scripted:
    def __init__(self, results, reads=None) -> None:
        self.results, self.sent = list(results), []
        self._reads = reads or (lambda req: {"items": []})

    def execute(self, op):
        self.sent.append(op)
        return self.results.pop(0)(op)

    def read(self, req):
        return self._reads(req)


def _not_sent(op):
    return OperationResult(
        request_id=op.request_id,
        status=FAILED,
        error_type="TransientServerError",
        error_message="not sent",
    )


def _nsf(op):
    return OperationResult(
        request_id=op.request_id,
        status=FAILED,
        http_status=400,
        error_type="InsufficientFunds",
        error_code="balance_insufficient",
    )


def _ok(op):
    return OperationResult(request_id=op.request_id, status=OK, provider_id="t1")


def test_not_sent_is_retried_with_the_same_request_id_and_a_later_refusal_is_failed():
    op = transfer_op()
    port = Scripted([_not_sent, _ok])
    store = InMemoryOperationStore()
    assert MutationExecutor(port, store, sleep=lambda s: None).run(op).status is OK
    assert [o.request_id for o in port.sent] == [op.request_id] * 2
    port = Scripted([_not_sent, _nsf])
    store = InMemoryOperationStore()
    result = MutationExecutor(port, store, sleep=lambda s: None).run(transfer_op(key="p2"))
    assert result.status is FAILED  # a request that never left makes nothing uncertain
    assert TransientServerError("x").retryable


def test_not_sent_then_crash_reads_back_first():
    op = transfer_op()
    store = InMemoryOperationStore()
    store.begin(op)
    store.record_attempt(op.request_id)
    store.complete(op.request_id, _not_sent(op))
    store.record_attempt(op.request_id)
    landed = {**op.body, "id": "t1", "status": "PROCESSING"}
    port = Scripted([], reads=lambda req: {"items": [landed]})
    result = MutationExecutor(port, store, sleep=lambda s: None, clock=after_lease).run(op)
    assert result.status is OK
    assert port.sent == []


@pytest.mark.parametrize(("found", "expected"), [("12.00", UNKNOWN), ("10.00", OK)])
def test_source_amount_is_checked_on_read_back(found, expected):
    base = transfer_op()
    body = {**base.body, "source_amount": Decimal("10.00")}
    op = make_op(
        "k3", base.name, base.path, {k: v for k, v in body.items() if k != "request_id"}, "src"
    )
    obj = {**op.body, "id": "t1", "status": "PROCESSING", "source_amount": Decimal(found)}

    def unknown(o):
        return OperationResult(request_id=o.request_id, status=UNKNOWN, error_type="OutcomeUnknown")

    port = Scripted([unknown], reads=lambda req: {"items": [obj]})
    result = MutationExecutor(port, InMemoryOperationStore(), sleep=lambda s: None).run(op)
    assert result.status is expected
    if expected is UNKNOWN:  # a mismatch is never FAILED
        assert result.error_code == "request_id_reused"


# ---- typed page errors and hidden objects -----------------------------------------------------


def _card(fake: FakeAirwallex) -> str:
    ch = fake.execute(
        make_op(
            "k2",
            "issuing.cardholders.create",
            "/api/v1/issuing/cardholders/create",
            {"type": "DELEGATE", "email": "poc@example.com"},
            "ch",
            with_request_id=False,
        )
    )
    return fake.execute(
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
                    "transaction_limits": {"currency": USD, "limits": []},
                },
            },
            "card",
        )
    ).provider_id


@pytest.mark.parametrize("query", [{"page_size": "ten"}, {"page_num": "x"}, {"page_num": "-1"}])
def test_bad_page_parameters_are_a_typed_error(query):
    fake = FakeAirwallex()
    card = _card(fake)
    with pytest.raises(ValidationFailed):
        fake.read(ReadRequest("/api/v1/issuing/transactions", {"card_id": card, **query}))
    with pytest.raises(ValidationFailed):
        find_card_transaction(fake, card_id=card, card_transaction_id="x", max_pages=0)


def test_simulations_never_act_on_hidden_objects():
    fake, op = setup()
    fake.inject(op.name, Fault.LIST_LAG, visible_after_s=LAG)
    tid = fake.execute(op).provider_id
    sim = make_op(
        "k3",
        "transfers.simulate_transition",
        f"/api/v1/simulation/transfers/{tid}/transition",
        {"next_status": "SENT"},
        "sim",
        with_request_id=False,
    )
    assert fake.execute(sim).error_type == "ValidationFailed"
    assert fake.objects("transfers")[0]["status"] == "SCHEDULED"
    hidden_card = FakeAirwallex(balances={USD: usd("10")}, list_lag_s=LAG)
    card = _card(hidden_card)
    with pytest.raises(AwxError):
        hidden_card.simulate("card_authorization", card_id=card, amount=Decimal("1"), currency=USD)
    assert hidden_card.objects("card_transactions") == []
    fake.advance(LAG)
    assert fake.execute(sim).provider_status == "SENT"


# ---- a retryable FAILED after an uncertain send is OUTCOME_UNKNOWN ----------------------------


def test_unknown_then_429_at_max_attempts_1_is_unknown():
    fake, op = setup()
    store = InMemoryOperationStore()
    fake.inject(op.name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=LAG)
    assert executor(fake, store, 1).run(op).status is UNKNOWN
    fake.inject(op.name, Fault.RATE_LIMIT)
    second = executor(fake, store, 1).run(op)
    assert second.status is UNKNOWN
    assert second.error_code is None
    assert second.http_status is None
    assert second.body["last_error"]["error_type"] == "RateLimited"
    assert "refused" not in second.body  # a 429 was never a refusal
    rec = store.get(op.request_id)
    assert (rec.status, rec.may_have_landed) == ("OUTCOME_UNKNOWN", True)


def test_unknown_then_not_sent_at_max_attempts_1_is_unknown():
    op = transfer_op()
    store = InMemoryOperationStore()

    def unknown(o):
        return OperationResult(request_id=o.request_id, status=UNKNOWN, error_type="OutcomeUnknown")

    for results in ([unknown], [_not_sent]):
        port = Scripted(results)
        result = MutationExecutor(port, store, sleep=lambda s: None, max_attempts=1).run(op)
    assert result.status is UNKNOWN
    assert result.body["last_error"]["error_type"] == "TransientServerError"
    assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"


def test_unknown_429_429_at_max_attempts_3_is_unknown():
    fake, op = setup()
    fake.inject(op.name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=LAG)
    fake.inject(op.name, Fault.RATE_LIMIT, times=2)
    store = InMemoryOperationStore()
    result = executor(fake, store, 3).run(op)
    assert result.status is UNKNOWN
    assert result.body["last_error"]["error_type"] == "RateLimited"
    assert store.get(op.request_id).status == "OUTCOME_UNKNOWN"
    assert len(fake.objects("transfers")) == 1


def test_certain_operations_keep_their_retryable_failed():
    """No change for a certain operation: a 429 exhausting the attempts is still FAILED."""
    fake, op = setup()
    fake.inject(op.name, Fault.RATE_LIMIT, times=3)
    store = InMemoryOperationStore()
    result = executor(fake, store, 3).run(op)
    assert (result.status, result.error_type) == (FAILED, "RateLimited")
    assert not store.get(op.request_id).may_have_landed


# ---- races and repeated listings --------------------------------------------------------------


class RacingStore(InMemoryOperationStore):
    """Another delivery stores a definitive result between begin and the crash-path complete."""

    def __init__(self, winner: OperationResult) -> None:
        super().__init__()
        self.winner, self.raced = winner, False

    def complete(self, request_id, result):
        if not self.raced and result.status is UNKNOWN:
            self.raced = True
            super().complete(request_id, self.winner)
            rec = self.get(request_id)
            return rec  # like DynamoDB: the first definitive result wins
        return super().complete(request_id, result)

    def record_attempt(self, request_id):
        rec = self.get(request_id)
        if rec is not None and rec.result is not None and rec.result.status is OK:
            raise ValueError("definitive")
        return super().record_attempt(request_id)


def test_crash_path_race_returns_the_definitive_record_without_sending():
    op = transfer_op()
    store = RacingStore(_ok(op))
    store.begin(op)
    InMemoryOperationStore.record_attempt(store, op.request_id)
    port = Scripted([])
    result = MutationExecutor(port, store, sleep=lambda s: None, clock=after_lease).run(op)
    assert result.status is OK
    assert port.sent == []


def test_the_same_object_listed_twice_is_one_object_and_ids_are_never_none():
    op = transfer_op()
    obj = {**op.body, "id": "t1", "status": "PROCESSING"}

    def unknown(o):
        return OperationResult(request_id=o.request_id, status=UNKNOWN, error_type="OutcomeUnknown")

    port = Scripted([unknown], reads=lambda req: {"items": [obj, dict(obj)]})
    result = MutationExecutor(port, InMemoryOperationStore(), sleep=lambda s: None).run(op)
    assert result.status is OK
    assert result.provider_id == "t1"
    no_id = {k: v for k, v in obj.items() if k != "id"}
    port = Scripted([unknown], reads=lambda req: {"items": [obj, no_id]})
    two = MutationExecutor(port, InMemoryOperationStore(), sleep=lambda s: None).run(op)
    assert two.error_code == "multiple_objects"
    assert "None" not in two.body["provider_ids"]
    assert "t1" in two.body["provider_ids"]


def test_dispute_simulation_refuses_a_hidden_payment_intent():
    fake = FakeAirwallex(online_payments_enabled=True, list_lag_s=LAG)
    pi = fake.execute(
        make_op(
            "k4",
            "payment_intents.create",
            "/api/v1/pa/payment_intents/create",
            {"amount": Decimal("5"), "currency": USD, "merchant_order_id": "poc"},
            "pi",
        )
    )
    fake.advance(LAG)
    fake.execute(
        make_op(
            "k4",
            "payment_intents.confirm",
            f"/api/v1/pa/payment_intents/{pi.provider_id}/confirm",
            {"payment_method": {"card": {"number": "4035501000000008"}}},
            "c",
        )
    )
    hidden = FakeAirwallex(online_payments_enabled=True)
    hp = hidden.execute(
        make_op(
            "k4",
            "payment_intents.create",
            "/api/v1/pa/payment_intents/create",
            {"amount": Decimal("5"), "currency": USD, "merchant_order_id": "poc"},
            "pi",
        )
    )
    hidden.execute(
        make_op(
            "k4",
            "payment_intents.confirm",
            f"/api/v1/pa/payment_intents/{hp.provider_id}/confirm",
            {"payment_method": {"card": {"number": "4035501000000008"}}},
            "c",
        )
    )
    hidden._state.kind("payment_intents")[hp.provider_id]["_visible_at"] = 10**9
    with pytest.raises(AwxError):
        hidden.simulate(
            "dispute",
            payment_intent_id=hp.provider_id,
            stage="RFI",
            reason_code="10.4",
            due_at="2026-12-01T00:00:00+0000",
        )
    assert hidden.objects("disputes") == []
    ok = fake.simulate(
        "dispute",
        payment_intent_id=pi.provider_id,
        stage="RFI",
        reason_code="10.4",
        due_at="2026-12-01T00:00:00+0000",
    )
    assert ok["status"] == "REQUIRES_RESPONSE"


def test_find_card_transaction_keeps_its_docstring():
    assert find_card_transaction.__doc__
    assert "transaction_id" in find_card_transaction.__doc__
