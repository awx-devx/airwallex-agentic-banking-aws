"""Provider request and response shapes: request_id in the accept and challenge bodies, the
reason enums, supporting_documents and product_type, the full status vocabulary, the simulated
dispute's due_at, the reconcile hook and the book id in sandbox setup keys."""

from __future__ import annotations

import pytest
from awx_common import RiskTier
from awx_common.store import InMemoryKitStore
from awx_common.workflow import ReconcileUndetermined
from awx_k4_dispute import SPECS
from awx_k4_dispute.config import DEFAULT_CONFIG
from awx_k4_dispute.finality import (
    ACCEPT_FINALITY,
    CHALLENGE_FINALITY,
    dispute_reconcile,
)
from awx_k4_dispute.provider import (
    PROVIDER_ACCEPT_REASON,
    PROVIDER_ACCEPT_REASONS,
    PROVIDER_CHALLENGE_REASON,
    PROVIDER_CHALLENGE_REASONS,
    AcceptReason,
    ChallengeReason,
    DisputeStatus,
    accept_operation,
    challenge_operation,
    parse_dispute,
)
from awx_k4_dispute.simulation import DEMO_CASES, stage_dispute
from k4_harness import (
    NOW,
    World,
    approve,
    challenge_input,
    run,
)

CHALLENGE = SPECS["challenge_dispute"]
ACCEPT = SPECS["accept_dispute"]
CLOSE = SPECS["close_dispute"]
H = "a" * 64
BOOK = "bk_0123456789abcdef"

# ---- request_id in the accept and challenge bodies ----------------------------------------------


def test_accept_and_challenge_bodies_carry_their_request_id():
    for reason in AcceptReason:
        op = accept_operation("dsp_1", "RFI", reason, H)
        assert op.body["request_id"] == op.request_id
    for reason in ChallengeReason:
        op = challenge_operation("dsp_1", "RFI", reason, [("PRIMARY", "file_1")], "X", "Y", H)
        assert op.body["request_id"] == op.request_id


def test_stub_refuses_a_body_without_request_id():
    port = World()
    port.add_dispute("d")
    op = accept_operation("d", "RFI", AcceptReason.LOW_VALUE, H)
    bare = type(op)(
        **{**op.__dict__, "body": {k: v for k, v in op.body.items() if k != "request_id"}}
    )
    result = port.port.execute(bare)
    assert result.status.value == "FAILED"
    assert port.disputes["d"]["status"] == "REQUIRES_RESPONSE"


# ---- reason enums, supporting_documents, product_type -------------------------------------------


def test_reason_maps_only_produce_api_reference_values():
    assert set(PROVIDER_ACCEPT_REASON.values()) <= PROVIDER_ACCEPT_REASONS
    assert set(PROVIDER_CHALLENGE_REASON.values()) <= PROVIDER_CHALLENGE_REASONS
    assert PROVIDER_ACCEPT_REASON[AcceptReason.LOW_VALUE] == "LOW_VALUE_TRANSACTION"


def test_challenge_body_groups_documents_by_type():
    op = challenge_operation(
        "d",
        "RFI",
        ChallengeReason.CARDHOLDER_AUTHORISED,
        [("ORDER", "f2"), ("PRIMARY", "f1"), ("ORDER", "f3")],
        "PHYSICAL_GOODS",
        "desc",
        H,
    )
    assert op.body["supporting_documents"] == {
        "documents": [
            {"type": "ORDER", "file_ids": ["f2", "f3"]},
            {"type": "PRIMARY", "file_ids": ["f1"]},
        ]
    }
    assert op.body["product_type"] == "PHYSICAL_GOODS"
    with pytest.raises(ValueError, match="document type"):
        challenge_operation(
            "d", "RFI", ChallengeReason.GOODS_DELIVERED, [("PROOF", "f")], "X", "Y", H
        )


def test_stub_refuses_challenge_without_product_type_at_chargeback():
    port = World()
    port.add_dispute("d", stage="CHARGEBACK")
    fid = port.fake.upload(b"%PDF-1.4 x", file_name="a.pdf", content_type="application/pdf")[
        "file_id"
    ]
    op = challenge_operation(
        "d", "CHARGEBACK", ChallengeReason.GOODS_DELIVERED, [("PRIMARY", fid)], "", "", H
    )
    assert port.port.execute(op).status.value == "FAILED"
    bad = challenge_operation(
        "d", "CHARGEBACK", ChallengeReason.GOODS_DELIVERED, [("PRIMARY", "nope")], "X", "", H
    )
    assert port.port.execute(bad).status.value == "FAILED"


def test_challenge_after_escalation_to_chargeback_sends_product_type():
    """The strong case escalated to CHARGEBACK; a policy-allowed challenge there must carry
    product_type or Airwallex refuses it. Exercised on a fresh dispute staged at CHARGEBACK."""
    port = World()
    port.add_dispute("d_cb", amount="900.00", reason={"code": "10.4"}, stage="CHARGEBACK")
    out = run(CHALLENGE, challenge_input("d_cb"), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail
    [op] = port.writes("disputes.challenge")
    assert op.body["product_type"] == DEFAULT_CONFIG.product_type


# ---- full status vocabulary ---------------------------------------------------------------------


def _view(status: str, **extra):
    body = {"id": "d", "amount": "900.00", "currency": "AUD", "status": status, **extra}
    return parse_dispute(body)


def test_every_documented_status_parses():
    for s in (
        "REQUIRES_RESPONSE",
        "CHALLENGED",
        "ACCEPTED",
        "REVERSED",
        "WON",
        "LOST",
        "PENDING_CLOSURE",
        "EXPIRED",
        "PENDING_DECISION",
    ):
        assert _view(s).status is DisputeStatus(s)


def test_pending_decision_counts_as_a_received_challenge():
    assert "PENDING_DECISION" in CHALLENGE_FINALITY.success
    assert "PENDING_CLOSURE" in ACCEPT_FINALITY.success
    assert "PENDING_CLOSURE" not in CHALLENGE_FINALITY.success


@pytest.mark.parametrize(
    ("status", "reason", "tier"),
    [
        ("REVERSED", "WON_EVIDENCE_ACCEPTED", RiskTier.AUTO),
        ("REVERSED", "LOST_ISSUER_REJECTED_EVIDENCE", RiskTier.BLOCKED),
        ("PENDING_CLOSURE", "ACCEPTED_LOW_VALUE", RiskTier.BLOCKED),
        ("PENDING_DECISION", "WON_EVIDENCE_ACCEPTED", RiskTier.BLOCKED),
    ],
)
def test_close_on_new_statuses(status, reason, tier):
    port = World()
    port.add_dispute("d", amount="900.00", status=status)
    out = run(CLOSE, {"dispute_id": "d", "close_reason": reason}, port=port)
    assert out.decision.risk_tier is tier
    if status == "REVERSED" and tier is RiskTier.AUTO:
        kinds = {e["kind"] for e in out.decision.computed["ledger_entries"]}
        assert kinds == {"DISPUTED_AMOUNT_RETAINED", "DISPUTE_FEE", "HANDLING_COST"}


def test_challenge_that_moves_to_pending_decision_succeeds():
    port = World()
    port.add_dispute("d", amount="900.00")
    original = port.fake.execute

    def to_pending(op):
        r = original(op)
        if op.name == "disputes.challenge":
            port.disputes["d"]["status"] = "PENDING_DECISION"
        return r

    port.fake.execute = to_pending
    out = run(CHALLENGE, challenge_input("d"), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail


# ---- due_at on the dispute simulation -----------------------------------------------------------


def test_stage_dispute_sends_code_derived_due_at():
    case = DEMO_CASES[0]
    op = stage_dispute(case, BOOK, "int_1", NOW)
    assert op.body["due_at"] == "2026-10-13T04:00:00Z"
    assert op.body["payment_intent_id"] == "int_1"
    with pytest.raises(ValueError, match="aware"):
        stage_dispute(case, BOOK, "int_1", NOW.replace(tzinfo=None))


# ---- StepSpec.reconcile wired in ----------------------------------------------------------------


def test_steps_carry_the_dispute_reconcile_hook():
    assert CHALLENGE.steps[1].reconcile is dispute_reconcile
    assert ACCEPT.steps[0].reconcile is dispute_reconcile
    assert CHALLENGE.on_abandon is None  # nothing of its own to release (M4)
    assert ACCEPT.on_abandon is None


def test_dispute_reconcile_outcomes():
    port = World()
    port.add_dispute("d")
    op = accept_operation("d", "RFI", AcceptReason.LOW_VALUE, H)
    assert dispute_reconcile(op, port.fake.read) is None
    port.disputes["d"]["status"] = "ACCEPTED"
    assert dispute_reconcile(op, port.fake.read)["id"] == "d"
    port.disputes["d"]["status"] = "LOST"
    with pytest.raises(ReconcileUndetermined):
        dispute_reconcile(op, port.fake.read)


# ---- M2: simulation keys carry the kit's book id ------------------------------------------------


def test_simulation_keys_carry_the_book_id():
    from awx_k4_dispute.cases import ensure_book
    from awx_k4_dispute.simulation import create_payment_intent

    store_a, store_b = InMemoryKitStore(), InMemoryKitStore()
    book_a, book_b = ensure_book(store_a), ensure_book(store_b)
    assert book_a == ensure_book(store_a)
    assert book_a != book_b
    case = DEMO_CASES[0]
    a, b = create_payment_intent(case, book_a), create_payment_intent(case, book_b)
    assert a.request_id != b.request_id
    assert book_a in a.body["merchant_order_id"]
    with pytest.raises(ValueError, match="book id"):
        create_payment_intent(case, "demo")
