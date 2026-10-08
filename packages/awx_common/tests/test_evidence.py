import hashlib

import pytest
from awx_common.evidence import (
    MAX_EVIDENCE_BYTES,
    EvidenceIntegrityError,
    EvidenceRef,
    EvidenceStore,
    InMemoryEvidenceStore,
    load_verified,
)

PDF = b"%PDF-1.7 poc-k4 test evidence"


def test_round_trip_and_verified_load():
    store = InMemoryEvidenceStore()
    assert isinstance(store, EvidenceStore)
    ref = store.put("ev_1", PDF, file_name="invoice.pdf", content_type="application/pdf")
    assert ref.sha256 == hashlib.sha256(PDF).hexdigest()
    assert EvidenceRef.from_body({"evidence_ref": ref.to_body()}) == ref
    assert load_verified(store, ref) == PDF


def test_tampered_or_missing_bytes_refused():
    store = InMemoryEvidenceStore()
    ref = store.put("ev_1", PDF, file_name="invoice.pdf", content_type="application/pdf")
    store.put("ev_1", PDF[:-1] + b"X", file_name="invoice.pdf", content_type="application/pdf")
    with pytest.raises(EvidenceIntegrityError, match="SHA-256"):
        load_verified(store, ref)
    store.put("ev_1", PDF + b"!", file_name="invoice.pdf", content_type="application/pdf")
    with pytest.raises(EvidenceIntegrityError, match="size"):
        load_verified(store, ref)
    with pytest.raises(EvidenceIntegrityError, match="not in the store"):
        load_verified(InMemoryEvidenceStore(), ref)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("evidence_id", "../etc", "evidence_id"),
        ("sha256", "ABC", "sha256"),
        ("content_type", "text/html", "content type"),
        ("size_bytes", 0, "range"),
        ("size_bytes", MAX_EVIDENCE_BYTES + 1, "range"),
        ("size_bytes", True, "int"),
        ("file_name", "../invoice.pdf", "bare file name"),
    ],
)
def test_reference_validation(field, value, message):
    fields = {
        "evidence_id": "ev_1",
        "sha256": "a" * 64,
        "file_name": "invoice.pdf",
        "content_type": "application/pdf",
        "size_bytes": 10,
    }
    fields[field] = value
    with pytest.raises(EvidenceIntegrityError, match=message):
        EvidenceRef(**fields)


def test_body_without_ref_refused():
    with pytest.raises(EvidenceIntegrityError, match="evidence_ref"):
        EvidenceRef.from_body({"notes": "x"})
