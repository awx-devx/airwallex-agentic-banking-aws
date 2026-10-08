"""The evidence upload handler (the kit's declared FUNCTIONS entry): typing, hashing and indexing
from the bytes, the S3 key it writes, input refusals and the synchronous payload cap."""

from __future__ import annotations

import base64
import hashlib

import pytest
from awx_common.store import InMemoryKitStore
from awx_k4_dispute import SPECS
from awx_k4_dispute.evidence import store_id
from awx_k4_dispute.functions import FUNCTIONS
from awx_k4_dispute.upload import UploadRejected, ingest_upload
from k4_harness import (
    FIXTURES,
    World,
    approve,
    challenge_input,
    evidence_text,
    run,
)

CHALLENGE = SPECS["challenge_dispute"]
ACCEPT = SPECS["accept_dispute"]
CLEAR = SPECS["clear_dispute_response"]

# ---- the evidence upload handler ------------------------------------------------------------


def _event(rel="strong_fraud/ev_order_history.pdf", **over):
    path = FIXTURES / rel
    ev = {
        "tenant_id": "platform",
        "dispute_id": "dsp_1",
        "evidence_id": path.stem,
        "file_name": path.name,
        "kind": "ORDER_HISTORY",
        "content_base64": base64.b64encode(path.read_bytes()).decode(),
        "text": evidence_text(rel),
    }
    ev.update(over)
    return ev


def test_functions_declares_exactly_the_upload_handler():
    (spec,) = FUNCTIONS
    assert spec.name == "evidence_upload"
    assert spec.handler == "awx_k4_dispute.upload.handler"
    assert {g.value for g in spec.grants} == {"EVIDENCE_PUT", "KIT_STORE"}
    spec.check_package("awx_k4_dispute")


def test_upload_indexes_from_the_bytes_and_writes_under_the_store_id():
    store, puts = InMemoryKitStore(), []
    out = ingest_upload(_event(), store=store, put_object=lambda *a: puts.append(a))
    data = (FIXTURES / "strong_fraud/ev_order_history.pdf").read_bytes()
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
    assert out["content_type"] == "application/pdf"
    assert out["text_indexed"] is True
    sid = store_id("dsp_1", "ev_order_history")
    assert puts == [(f"evidence/k4/platform/{sid}", data, "application/pdf")]
    assert store.get("evidence/dsp_1/ev_order_history").value["sha256"] == out["sha256"]
    # idempotent for the same bytes
    assert ingest_upload(_event(), store=store, put_object=lambda *a: puts.append(a)) == out


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"content_base64": "!!"}, "base64"),
        ({"content_base64": ""}, "empty|base64"),
        ({"kind": "SELFIE"}, "kind"),
        ({"tenant_id": "../x"}, "tenant_id"),
        ({"tenant_id": "merchant-demo"}, "platform"),
        ({"dispute_id": "a/b"}, "dispute_id"),
        ({"evidence_id": "nope"}, "evidence_id"),
        ({"sha256": "a" * 64}, "unexpected"),  # metadata is never taken from the caller
        ({"content_type": "application/pdf"}, "unexpected"),
    ],
)
def test_upload_rejects_bad_input_and_writes_nothing(over, match):
    store, puts = InMemoryKitStore(), []
    with pytest.raises(UploadRejected, match=match):
        ingest_upload(_event(**over), store=store, put_object=lambda *a: puts.append(a))
    assert puts == []
    assert store.query("") == []


def test_upload_refuses_png_and_different_bytes_under_an_existing_id():
    store, puts = InMemoryKitStore(), []
    png = base64.b64encode((FIXTURES / "rejected/ev_receipt.png").read_bytes()).decode()
    with pytest.raises(UploadRejected, match="PNG"):
        ingest_upload(
            _event(evidence_id="ev_receipt", file_name="r.jpg", content_base64=png),
            store=store,
            put_object=lambda *a: puts.append(a),
        )
    ingest_upload(_event(), store=store, put_object=lambda *a: puts.append(a))
    other = base64.b64encode(
        (FIXTURES / "strong_fraud/ev_order_history.pdf").read_bytes() + b"\n"
    ).decode()
    with pytest.raises(UploadRejected, match="different bytes"):
        ingest_upload(
            _event(content_base64=other), store=store, put_object=lambda *a: puts.append(a)
        )
    assert len(puts) == 1


def test_uploaded_evidence_drives_a_challenge_through_the_runner():
    """End to end: the handler's index and bytes are what the challenge uploads and cites."""
    w = World(auto_ingest=False)
    w.add_dispute("dsp_up", amount="900.00", reason={"code": "10.4"})
    keys = []

    def put(key, data, ct):
        # The adapter's S3EvidenceStore reads evidence/k4/<run tenant>/<evidence_id>; kit 4
        # runs as the platform, so the full key must be exactly that.
        prefix = "evidence/k4/platform/"
        assert key.startswith(prefix)
        keys.append(key)
        w.evidence.put(key.removeprefix(prefix), data, file_name="x", content_type=ct)

    for rel, kind in (
        ("strong_fraud/ev_order_history.pdf", "ORDER_HISTORY"),
        ("strong_fraud/ev_signature.jpg", "DELIVERY_PROOF"),
    ):
        ingest_upload(
            _event(rel, dispute_id="dsp_up", kind=kind), store=w.kit_store, put_object=put
        )
    assert keys == [
        f"evidence/k4/platform/{store_id('dsp_up', e)}"
        for e in ("ev_order_history", "ev_signature")
    ]
    out = run(CHALLENGE, challenge_input("dsp_up"), port=w, decide=approve("a-1"))
    assert out.state == "SUCCEEDED", out.detail
    assert out.decision.computed["quotes_checked"] is True
    assert len(w.writes("files.upload")) == 2


def test_upload_is_capped_at_the_synchronous_payload_bound():
    from awx_k4_dispute.upload import MAX_UPLOAD_BYTES

    big = b"%PDF-1.4\n" + b"0" * MAX_UPLOAD_BYTES
    store, puts = InMemoryKitStore(), []
    ev = _event(content_base64=base64.b64encode(big).decode(), text="")
    with pytest.raises(UploadRejected, match="synchronous invoke"):
        ingest_upload(ev, store=store, put_object=lambda *a: puts.append(a))
    assert puts == []
    assert store.query("") == []
    assert len(base64.b64encode(b"0" * MAX_UPLOAD_BYTES)) < 6 * 1024 * 1024
