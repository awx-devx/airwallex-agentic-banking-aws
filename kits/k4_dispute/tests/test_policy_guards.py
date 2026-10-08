"""Policy guards: policy never writes, evidence metadata comes from the code-owned index, every
operation passes the tenant scope check, close reasons bind to the accept used, the agent floor,
per-build deadlines, quote checks against extracted text, whole book ids, and a proposal over
another run's crashed attempt."""

from __future__ import annotations

from datetime import timedelta

import pytest
from awx_common import RiskTier
from awx_common.evidence import FILES_UPLOAD_OP, EvidenceRef
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import check_tenant_scope
from awx_k4_dispute import SPECS
from awx_k4_dispute.cases import check_book_id, ensure_book, record_attempt, take_lock
from awx_k4_dispute.config import DEFAULT_CONFIG
from awx_k4_dispute.evidence import EvidenceError, EvidenceKind, ingest_evidence
from awx_k4_dispute.provider import (
    AcceptReason,
    ChallengeReason,
    accept_operation,
    challenge_operation,
    parse_dispute,
    upload_operation,
)
from awx_k4_dispute.simulation import DEMO_CASES, create_payment_intent
from k4_harness import (
    FIXTURES,
    NOW,
    TENANT,
    World,
    approve,
    challenge_input,
    make_ctx,
    money,
    run,
)

CHALLENGE = SPECS["challenge_dispute"]
ACCEPT = SPECS["accept_dispute"]
CLOSE = SPECS["close_dispute"]
H = "a" * 64
BOOK = "bk_0123456789abcdef"

CHALLENGE_TTL = __import__("datetime").timedelta(hours=1)

# ---- policy is pure (crash, redelivery and on_abandon are in test_scenarios) -------------------


def test_policy_never_writes_the_store():
    port = World()
    port.add_dispute("d", amount="900.00")
    from k4_harness import make_ctx

    ctx = make_ctx(port, CHALLENGE, NOW)  # auto-ingest happens here
    before = [(i.key, i.version) for i in port.kit_store.query("")]
    CHALLENGE.policy(CHALLENGE.validate(challenge_input("d")), ctx)
    ACCEPT.policy(
        ACCEPT.validate(
            {
                "dispute_id": "d",
                "disputed_amount": money("900.00"),
                "reason": "INSUFFICIENT_EVIDENCE",
                "rationale": "r",
            }
        ),
        ctx,
    )
    assert [(i.key, i.version) for i in port.kit_store.query("")] == before


# ---- evidence metadata is code-owned ------------------------------------------------------------


def _manual_port():
    port = World(auto_ingest=False)
    port.add_dispute("d_a", amount="900.00")
    port.add_dispute("d_b", amount="900.00")
    return port


def test_evidence_not_indexed_for_this_dispute_is_blocked():
    port = _manual_port()
    for rel in ("strong_fraud/ev_order_history.pdf", "strong_fraud/ev_signature.jpg"):
        port.ingest("d_b", rel)  # filed under the OTHER dispute
    out = run(CHALLENGE, challenge_input("d_a"), port=port, decide=approve("approver-1"))
    assert out.decision.risk_tier is RiskTier.BLOCKED
    assert any("not in this dispute's evidence index" in r for r in out.decision.reasons)
    assert port.writes() == []


def test_upload_ref_comes_from_the_index_not_the_model():
    port = _manual_port()
    refs = [
        port.ingest("d_a", r)
        for r in ("strong_fraud/ev_order_history.pdf", "strong_fraud/ev_signature.jpg")
    ]
    out = run(CHALLENGE, challenge_input("d_a"), port=port, decide=approve("approver-1"))
    assert out.state == "SUCCEEDED", out.detail
    sent = {u.sha256 for u in port.writes(FILES_UPLOAD_OP)}  # the bytes the executor loaded
    assert sent == {r.file.sha256 for r in refs}
    sids = {e["store_id"] for e in out.proposal.computed["evidence"]}
    assert sids == {r.store_id for r in refs}


def test_ingest_types_from_bytes_and_refuses_png_and_changed_bytes():
    store = InMemoryKitStore()
    png = (FIXTURES / "rejected/ev_receipt.png").read_bytes()
    with pytest.raises(EvidenceError, match="PNG"):
        ingest_evidence(
            store, "d", "ev_receipt", "ev_receipt.jpg", png, EvidenceKind.OTHER, DEFAULT_CONFIG
        )
    pdf = (FIXTURES / "strong_fraud/ev_order_history.pdf").read_bytes()
    first = ingest_evidence(
        store, "d", "ev_x", "x.pdf", pdf, EvidenceKind.ORDER_HISTORY, DEFAULT_CONFIG
    )
    assert first.file.content_type == "application/pdf"
    again = ingest_evidence(
        store, "d", "ev_x", "x.pdf", pdf, EvidenceKind.ORDER_HISTORY, DEFAULT_CONFIG
    )
    assert again == first
    with pytest.raises(EvidenceError, match="different bytes"):
        ingest_evidence(
            store, "d", "ev_x", "x.pdf", pdf + b"\n", EvidenceKind.ORDER_HISTORY, DEFAULT_CONFIG
        )


# ---- tenant scope -------------------------------------------------------------------------------


def test_every_operation_the_kit_builds_passes_the_tenant_scope_check():
    ref = EvidenceRef("k4e_x", "b" * 64, "a.pdf", "application/pdf", 10)
    ops = [
        *(accept_operation("d", "RFI", r, H) for r in AcceptReason),
        *(
            challenge_operation("d", "RFI", r, [("PRIMARY", "f")], "X", "Y", H)
            for r in ChallengeReason
        ),
        upload_operation("d", "ev_a", ref, H, book_id=BOOK, run_id="pid"),
    ]
    for op in ops:
        check_tenant_scope(op, TENANT)
        assert op.on_behalf_of is None


# ---- close reason bound to the accept reason used -----------------------------------------------


def test_close_reason_must_match_the_accept_used():
    port = World()
    port.add_dispute("d", amount="15.00", reason={"code": "13.1"}, stage="CHARGEBACK")
    raw = {
        "dispute_id": "d",
        "disputed_amount": money("15.00"),
        "reason": "LOW_VALUE",
        "rationale": "r",
    }
    assert run(ACCEPT, raw, port=port, decide=approve("approver-1")).state == "SUCCEEDED"
    wrong = {"dispute_id": "d", "close_reason": "ACCEPTED_MERCHANT_ERROR"}
    assert run(CLOSE, wrong, port=port).decision.risk_tier is RiskTier.BLOCKED
    right = {"dispute_id": "d", "close_reason": "ACCEPTED_LOW_VALUE"}
    assert run(CLOSE, right, port=port).state == "SUCCEEDED"


# ---- refer and close are AUTO even when the model starts them; accept and challenge are not -----


def test_agent_floor():
    assert SPECS["refer_dispute"].agent_min_tier is RiskTier.AUTO
    assert SPECS["close_dispute"].agent_min_tier is RiskTier.AUTO
    assert ACCEPT.agent_min_tier is RiskTier.SINGLE
    assert CHALLENGE.agent_min_tier is RiskTier.SINGLE


def test_deadline_is_checked_against_each_builds_own_now():
    port = World()
    port.add_dispute("d", amount="900.00", due_at=(NOW + timedelta(hours=30)).isoformat())
    from awx_common.workflow import PolicyDecision, StepContext
    from awx_k4_dispute.deadlines import DeadlineMissed
    from k4_harness import make_ctx

    intent = CHALLENGE.validate(challenge_input("d"))
    decision = CHALLENGE.policy(intent, make_ctx(port, CHALLENGE, NOW))
    late = make_ctx(port, CHALLENGE, NOW + timedelta(hours=6))
    with pytest.raises(DeadlineMissed):
        CHALLENGE.steps[0].build(StepContext(intent, decision, late, H, {}, "pid-test"))
    assert isinstance(decision, PolicyDecision)


def test_extracted_evidence_text_checks_every_cited_quote(port, strong_case):
    """challenge_policy gets the extracted text from the evidence index (texts=),
    so quotes are checked against document content, not just their citation."""
    out = run(CHALLENGE, challenge_input(strong_case), port=port)
    assert out.decision.computed["quotes_checked"] is True
    assert out.decision.computed["rejected_facts"] == []
    port2_raw = challenge_input(strong_case)
    port2_raw["facts"] = [
        *port2_raw["facts"],
        {"kind": "SIGNED_DELIVERY", "evidence_id": "ev_order_history", "quote": "signed by Bob"},
    ]
    out2 = run(CHALLENGE, port2_raw, port=port)
    assert any("quote does not appear" in r for r in out2.decision.computed["rejected_facts"])


def test_book_id_is_validated_whole():
    """The whole book id is checked, not just the `bk_` prefix."""
    store = InMemoryKitStore()
    book = ensure_book(store)
    assert check_book_id(book) == book
    for bad in (
        "bk_",
        "bk_demo",
        "bk_0123456789ABCDEF",
        "bk_0123456789abcde",
        "xbk_0123456789abcdef",
        "bk_0123456789abcdef0",
        None,
    ):
        with pytest.raises(ValueError, match="book id"):
            check_book_id(bad)
        with pytest.raises(ValueError, match="book id"):
            create_payment_intent(DEMO_CASES[0], bad)  # type: ignore[arg-type]
    store.put("book", {"book_id": "demo"}, expected_version=1)
    with pytest.raises(ValueError, match="book id"):
        ensure_book(store)


def test_identical_reproposal_over_a_crashed_attempt_is_refused_at_proposal_time(port, small_case):
    """Another run's crashed attempt (lock held, no response) makes even an
    identical proposal BLOCKED in policy, before any approval is asked for."""
    raw = {
        "dispute_id": small_case,
        "disputed_amount": {"amount": "15.00", "currency": "AUD"},
        "reason": "LOW_VALUE",
        "rationale": "small",
    }
    spec = SPECS["accept_dispute"]
    intent = spec.validate(raw)
    ctx = make_ctx(port, spec)
    decision = spec.policy(intent, ctx)
    assert decision.risk_tier is RiskTier.SINGLE
    view = parse_dispute(port.disputes[small_case])
    take_lock(port.kit_store, small_case, "crashed-run", "accept", view, NOW, CHALLENGE_TTL)
    record_attempt(port.kit_store, small_case, "crashed-run", "accept", intent, decision, "rid")
    asked: list = []
    out = run(spec, raw, port=port, decide=lambda p: asked.append(p) or [])
    assert out.state == "BLOCKED"
    assert asked == []
    assert any("never finished" in r for r in out.decision.reasons)
