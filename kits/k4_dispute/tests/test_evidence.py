"""Evidence pack: file checks, citations, strength bands and the injection fixture."""

from __future__ import annotations

import hashlib

import pytest
from awx_k4_dispute.config import DEFAULT_CONFIG, StrengthBand
from awx_k4_dispute.evidence import (
    EvidenceError,
    EvidenceFile,
    EvidenceKind,
    Fact,
    FactKind,
    assess,
    check_evidence_file,
    inspect_bytes,
    instruction_markers,
    minimum_evidence_gaps,
    verify_facts,
)
from awx_k4_dispute.intents import parse_facts
from awx_k4_dispute.provider import ReasonFamily
from hypothesis import given
from hypothesis import strategies as st
from k4_harness import FIXTURES, STRONG_FACTS, STRONG_FILES, evidence_file, evidence_text

ALL_DOCS = [
    "strong_fraud/ev_signature.jpg",
    "strong_fraud/ev_order_history.pdf",
    "small_not_received/ev_delivery_scan.jpg",
    "credit_not_processed/ev_support_emails.pdf",
    "injection/ev_customer_letter.pdf",
]


@pytest.mark.parametrize("rel", ALL_DOCS)
def test_fixture_files_are_real_jpg_or_pdf(rel):
    ev = evidence_file(rel)
    data = (FIXTURES / rel).read_bytes()
    assert ev.sha256 == hashlib.sha256(data).hexdigest()
    assert ev.size_bytes == len(data)
    assert ev.content_type == ("application/pdf" if rel.endswith(".pdf") else "image/jpeg")


def test_png_is_rejected_by_content_not_name():
    data = (FIXTURES / "rejected/ev_receipt.png").read_bytes()
    with pytest.raises(EvidenceError, match="PNG"):
        inspect_bytes("ev_receipt", "ev_receipt.jpg", data, EvidenceKind.OTHER, DEFAULT_CONFIG)


def test_content_type_comes_from_magic_bytes():
    pdf = (FIXTURES / "strong_fraud/ev_order_history.pdf").read_bytes()
    with pytest.raises(EvidenceError, match="file name"):
        inspect_bytes("ev_x", "x.jpg", pdf, EvidenceKind.OTHER, DEFAULT_CONFIG)
    with pytest.raises(EvidenceError, match="not a JPG or PDF"):
        inspect_bytes("ev_x", "x.pdf", b"GIF89a....", EvidenceKind.OTHER, DEFAULT_CONFIG)


@pytest.mark.parametrize(
    "over",
    [
        {"evidence_id": "bad id"},
        {"sha256": "A" * 64},
        {"content_type": "image/png", "file_name": "a.png"},
        {"file_name": "../a.pdf"},
        {"size_bytes": 0},
        {"size_bytes": DEFAULT_CONFIG.max_evidence_bytes + 1},
    ],
)
def test_check_evidence_file_rejects(over):
    base = dict(
        evidence_id="ev_a",
        file_name="a.pdf",
        content_type="application/pdf",
        sha256="a" * 64,
        size_bytes=10,
        kind=EvidenceKind.OTHER,
    )
    base.update(over)
    with pytest.raises(EvidenceError):
        check_evidence_file(EvidenceFile(**base), DEFAULT_CONFIG)


def _strong():
    files = [evidence_file(f) for f in STRONG_FILES]
    texts = {f.evidence_id: evidence_text(rel) for f, rel in zip(files, STRONG_FILES, strict=True)}
    return files, parse_facts(STRONG_FACTS), texts


def test_strong_fraud_pack_is_strong_with_quotes_checked():
    files, facts, texts = _strong()
    check = verify_facts(facts, files, texts)
    assert check.quotes_checked
    assert not check.rejected
    a = assess(ReasonFamily.FRAUD_CARD_ABSENT, check.verified, DEFAULT_CONFIG)
    assert a.band is StrengthBand.STRONG
    assert a.prior_orders == 3
    assert minimum_evidence_gaps(ReasonFamily.FRAUD_CARD_ABSENT, files, check.verified) == ()


def test_invented_quote_and_foreign_citation_are_rejected():
    files, facts, texts = _strong()
    fake = Fact(FactKind.SIGNED_DELIVERY, "ev_order_history", "Signed by the cardholder")
    foreign = Fact(FactKind.IP_MATCH_PRIOR, "ev_elsewhere", "IP matches")
    check = verify_facts([*facts, fake, foreign], files, texts)
    reasons = dict((f.evidence_id + f.quote, why) for f, why in check.rejected)
    assert "quote does not appear" in reasons["ev_order_historySigned by the cardholder"]
    assert "not in the evidence pack" in reasons["ev_elsewhereIP matches"]


def test_without_the_signature_the_case_is_moderate():
    files, facts, texts = _strong()
    check = verify_facts([f for f in facts if f.kind is not FactKind.SIGNED_DELIVERY], files, texts)
    assert assess(ReasonFamily.FRAUD_CARD_ABSENT, check.verified, DEFAULT_CONFIG).band is (
        StrengthBand.MODERATE
    )


def test_small_and_credit_cases():
    weak = assess(
        ReasonFamily.NOT_RECEIVED,
        [Fact(FactKind.DELIVERY_SCAN_NO_SIGNATURE, "ev_delivery_scan", "No signature obtained")],
        DEFAULT_CONFIG,
    )
    assert weak.band is StrengthBand.WEAK
    fault = assess(
        ReasonFamily.CREDIT_NOT_PROCESSED,
        [
            Fact(FactKind.CUSTOMER_CONTACTED_SUPPORT, "ev_s", "a"),
            Fact(FactKind.CUSTOMER_CONTACTED_SUPPORT, "ev_s", "b"),
            Fact(FactKind.NO_SUPPORT_REPLY, "ev_s", "c"),
            Fact(FactKind.CANCELLATION_POLICY_ACCEPTED, "ev_s", "d"),
        ],
        DEFAULT_CONFIG,
    )
    assert fault.merchant_fault
    assert fault.support_contacts == 2
    assert fault.band is StrengthBand.WEAK


def test_contradictory_delivery_evidence_is_capped():
    a = assess(
        ReasonFamily.NOT_RECEIVED,
        [
            Fact(FactKind.SIGNED_DELIVERY, "ev_a", "x"),
            Fact(FactKind.DELIVERY_SCAN_NO_SIGNATURE, "ev_a", "y"),
        ],
        DEFAULT_CONFIG,
    )
    assert a.contradictory
    assert a.band is StrengthBand.WEAK


@given(refs=st.lists(st.sampled_from(["ORD-1", "ord-1", "ORD-2", " ORD-3 "]), max_size=12))
def test_prior_orders_counts_distinct_order_refs(refs):
    facts = [Fact(FactKind.PRIOR_UNDISPUTED_ORDER, "ev_o", f"q{i}", r) for i, r in enumerate(refs)]
    a = assess(ReasonFamily.FRAUD_CARD_ABSENT, facts, DEFAULT_CONFIG)
    assert a.prior_orders == len({r.strip().casefold() for r in refs})


def test_minimum_evidence_gaps():
    files, facts, _ = _strong()
    gaps = minimum_evidence_gaps(ReasonFamily.FRAUD_CARD_ABSENT, files[:1], [])
    assert any("at least 2" in g for g in gaps)
    assert any("no verified fact" in g for g in gaps)
    assert minimum_evidence_gaps(ReasonFamily.UNKNOWN, files, facts)


def test_injection_fixture_is_flagged_and_clean_fixtures_are_not():
    assert set(instruction_markers(evidence_text("injection/ev_customer_letter.pdf"))) >= {
        "override",
        "role",
        "accept",
        "refund",
        "approval",
    }
    for rel in ALL_DOCS[:4]:
        assert instruction_markers(evidence_text(rel)) == (), rel
    # The marker text survives in the PDF bytes, so any OCR or model extraction sees it too.
    raw = (FIXTURES / "injection/ev_customer_letter.pdf").read_bytes()
    assert b"ignore all previous instructions" in raw
