"""S3EvidenceStore against moto: key layout, scoping, and load_verified on top of it."""

from __future__ import annotations

import hashlib

import boto3
import pytest
from awx_common.evidence import EvidenceIntegrityError, EvidenceRef, EvidenceStore, load_verified
from awx_spine_aws.evidence import S3EvidenceStore, evidence_key
from moto import mock_aws

PDF = b"%PDF-1.7 poc-s2 evidence"


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(
            Bucket="artifacts", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        client.put_object(Bucket="artifacts", Key=evidence_key("k4", "platform", "ev_1"), Body=PDF)
        yield client


def ref(data=PDF) -> EvidenceRef:
    return EvidenceRef(
        "ev_1", hashlib.sha256(data).hexdigest(), "a.pdf", "application/pdf", len(data)
    )


def test_key_layout():
    assert evidence_key("k4", "tenant-a", "ev_1") == "evidence/k4/tenant-a/ev_1"


def test_reads_and_verifies(s3):
    store = S3EvidenceStore("artifacts", kit="k4", tenant_id="platform", client=s3)
    assert isinstance(store, EvidenceStore)
    assert load_verified(store, ref()) == PDF


def test_another_tenant_or_kit_cannot_see_it(s3):
    for kit, tenant in (("k4", "tenant-a"), ("k8", "platform")):
        store = S3EvidenceStore("artifacts", kit=kit, tenant_id=tenant, client=s3)
        with pytest.raises(KeyError):
            store.get_bytes("ev_1")


def test_tampered_bytes_are_refused(s3):
    store = S3EvidenceStore("artifacts", kit="k4", tenant_id="platform", client=s3)
    with pytest.raises(EvidenceIntegrityError):
        load_verified(store, ref(b"%PDF-1.7 something else!"))


def test_path_tricks_are_refused(s3):
    store = S3EvidenceStore("artifacts", kit="k4", tenant_id="platform", client=s3)
    with pytest.raises(KeyError):
        store.get_bytes("../k8/platform/ev_1")
    with pytest.raises(ValueError, match="tenant_id"):
        S3EvidenceStore("artifacts", kit="k4", tenant_id="a/b", client=s3)
