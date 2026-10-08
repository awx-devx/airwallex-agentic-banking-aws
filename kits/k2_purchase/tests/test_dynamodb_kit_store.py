"""Kit 2's book id and change numbers on the real DynamoDbKitStore (moto), through the real
runner. Both rely on KitStore.put(expected_version=None) being a conditional create:
of two concurrent creates of one key exactly one passes."""

from __future__ import annotations

import boto3
import pytest
from awx_common import RiskTier
from awx_common.workflow import PolicyDecision, StepContext
from awx_k2_purchase.workflow import BOOK_KEY, StaleApproval, claim_change, ensure_book
from awx_spine_aws.kit_store import DynamoDbKitStore
from k2env import K2Env
from moto import mock_aws


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        client = boto3.client("dynamodb")
        client.create_table(
            TableName="ledger",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def ddb(db) -> DynamoDbKitStore:
    return DynamoDbKitStore("ledger", kit="k2", tenant_id="platform", client=db)


def test_issue_freeze_unfreeze_freeze_through_the_runner_on_dynamodb(db):
    env = K2Env(kit_store=ddb(db))
    _, card_id = env.issue()
    assert env.run("freeze_card", {"card_id": card_id}).state == "SUCCEEDED"
    assert env.run("unfreeze_card", {"card_id": card_id}, "approver").state == "SUCCEEDED"
    assert env.run("freeze_card", {"card_id": card_id}).state == "SUCCEEDED"
    assert env.status(card_id) == "INACTIVE"
    claims = ddb(db).query(f"card-change:{card_id}:")
    assert [c.key.rsplit(":", 1)[1] for c in claims] == ["000001", "000002", "000003"]
    assert len({c.value["owner"] for c in claims}) == 3


def test_limit_test_through_the_runner_on_dynamodb(db):
    env = K2Env(kit_store=ddb(db))
    card_id = env.limit_test_card()
    out = env.run("run_limit_test", {"card_id": card_id}, "approver")
    assert out.state == "SUCCEEDED", (out.detail, out.postcondition)


class _RacingStore:
    """Another run creates `key` between our get (which saw nothing) and our create."""

    def __init__(self, inner, key, value):
        self.inner, self.key, self.value, self.raced = inner, key, value, False

    def get(self, key):
        found = self.inner.get(key)
        if key == self.key and found is None and not self.raced:
            self.raced = True
            self.inner.put(key, self.value, expected_version=None)
        return found

    def put(self, key, value, *, expected_version):
        return self.inner.put(key, value, expected_version=expected_version)

    def query(self, prefix):
        return self.inner.query(prefix)


def _step(env: K2Env, store, proposal_id: str, computed=None) -> StepContext:
    from awx_common.workflow import PolicyContext

    ctx = PolicyContext(env.ctx().tenant, env.now(), env.fake.read, store=store)
    decision = PolicyDecision(RiskTier.AUTO, ("x",), computed or {"change_seq": 1})
    return StepContext({"card_id": "c1"}, decision, ctx, "a" * 64, {}, proposal_id)


def test_two_runs_racing_for_one_change_number_never_share_it(db):
    env = K2Env()
    racing = _RacingStore(ddb(db), "card-change:c1:000001", {"owner": "the-other-run"})
    with pytest.raises(StaleApproval, match="was not applied"):
        claim_change(_step(env, racing, "this-run"), "c1")
    (claim,) = ddb(db).query("card-change:c1:")
    assert claim.value["owner"] == "the-other-run"


def test_two_first_builds_agree_on_one_book(db):
    env = K2Env()
    racing = _RacingStore(ddb(db), BOOK_KEY, {"book_id": "bk-" + "9" * 32})
    book = ensure_book(_step(env, racing, "this-run", {}))
    assert book == "bk-" + "9" * 32
    assert ddb(db).get(BOOK_KEY).value["book_id"] == book
