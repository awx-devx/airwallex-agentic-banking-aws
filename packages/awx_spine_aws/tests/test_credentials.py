"""SecretsManagerCredentials with botocore Stubber: caching and redacted errors."""

from __future__ import annotations

import json

import boto3
import pytest
from awx_spine_aws.credentials import CredentialsUnavailable, SecretsManagerCredentials
from botocore.stub import Stubber

ARN = "arn:aws:secretsmanager:ap-southeast-2:111122223333:secret:awx-AbCdEf"
FAKE_KEY = "not-a-real-key-0000"


def provider(responses, clock):
    client = boto3.client("secretsmanager", region_name="ap-southeast-2")
    stub = Stubber(client)
    for resp in responses:
        if isinstance(resp, str):
            stub.add_client_error("get_secret_value", service_error_code=resp)
        else:
            stub.add_response("get_secret_value", resp, {"SecretId": ARN})
    stub.activate()
    return SecretsManagerCredentials(ARN, client=client, clock=clock), stub


def secret(**fields):
    return {"ARN": ARN, "Name": "awx", "SecretString": json.dumps(fields)}


def test_reads_and_caches_for_five_minutes():
    t = [0.0]
    creds, stub = provider(
        [secret(client_id="cid", api_key=FAKE_KEY), secret(client_id="cid2", api_key=FAKE_KEY)],
        lambda: t[0],
    )
    assert creds.get().client_id == "cid"
    t[0] = 299.0
    assert creds.get().client_id == "cid"  # cached
    t[0] = 300.0
    assert creds.get().client_id == "cid2"  # refreshed
    stub.assert_no_pending_responses()


def test_invalidate_forces_a_refetch():
    creds, _ = provider(
        [secret(client_id="a", api_key=FAKE_KEY), secret(client_id="b", api_key=FAKE_KEY)],
        lambda: 0.0,
    )
    creds.get()
    creds.invalidate()
    assert creds.get().client_id == "b"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"ARN": ARN, "Name": "awx"}, "not been seeded"),
        ({"ARN": ARN, "Name": "awx", "SecretString": "not json " + FAKE_KEY}, "not JSON"),
        (secret(client_id="cid"), "needs client_id and api_key"),
        ("ResourceNotFoundException", "does not exist"),
    ],
)
def test_errors_name_the_problem_never_the_value(response, message):
    creds, _ = provider([response], lambda: 0.0)
    with pytest.raises(CredentialsUnavailable, match=message) as info:
        creds.get()
    assert FAKE_KEY not in str(info.value)
    chained = info.value.__context__ if not info.value.__suppress_context__ else None
    assert info.value.__cause__ is None and chained is None  # no inner error carries the value


def test_repr_has_no_value():
    creds, _ = provider([secret(client_id="cid", api_key=FAKE_KEY)], lambda: 0.0)
    value = creds.get()
    assert FAKE_KEY not in repr(creds) and FAKE_KEY not in repr(value)
