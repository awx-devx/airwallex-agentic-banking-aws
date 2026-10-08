"""Two bridges built at the same time cannot both take the last room
above the reserve floor on the platform wallet. Run through the real runner on both
InMemoryKitStore and DynamoDbKitStore (moto)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import boto3
import pytest
from awx_common.store import InMemoryKitStore, KitStore, StoredItem
from awx_k5_platform_spend.ledger import LEDGER_KEY
from awx_spine_aws.store import DynamoDbKitStore
from k5_support import Platform, obligation, usd
from moto import mock_aws


class Racing:
    """A KitStore that runs `interleave` once, just before the next ledger write lands."""

    def __init__(self, inner: KitStore) -> None:
        self.inner = inner
        self.interleave: Callable[[], None] | None = None

    def get(self, key: str) -> StoredItem | None:
        return self.inner.get(key)

    def query(self, prefix: str):
        return self.inner.query(prefix)

    def put(self, key: str, value: Mapping[str, Any], *, expected_version: int | None):
        if key == LEDGER_KEY and self.interleave is not None:
            fn, self.interleave = self.interleave, None
            fn()
        return self.inner.put(key, value, expected_version=expected_version)


@pytest.fixture
def dynamo(monkeypatch):
    for k, v in (
        ("AWS_ACCESS_KEY_ID", "testing"),
        ("AWS_SECRET_ACCESS_KEY", "testing"),
        ("AWS_DEFAULT_REGION", "ap-southeast-2"),
    ):
        monkeypatch.setenv(k, v)
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-southeast-2")
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


def _store(kind: str, dynamo_client) -> KitStore:
    if kind == "memory":
        return InMemoryKitStore()
    return DynamoDbKitStore("ledger", kit="k5", tenant_id="platform", client=dynamo_client)


@pytest.fixture(params=["memory", "dynamodb"])
def store_kind(request):
    return request.param


def _race_platform(inner: KitStore) -> tuple[Platform, Racing]:
    racing = Racing(inner)
    # Wallet room 700 - 600 = 100; ledger room 1000 - 600 = 400; two 80 USD shortfalls.
    p = Platform(
        platform_usd="700",
        capital="1000",
        floor="600",
        obligations=(obligation("t-alpha", "ob-a1", "280"), obligation("t-bravo", "ob-b1", "230")),
        platform_store=racing,
    )
    return p, racing


@pytest.mark.parametrize("a_finishes", [True, False], ids=["a-settled", "a-in-flight"])
def test_two_concurrent_bridges_never_breach_the_wallet_floor(request, store_kind, a_finishes):
    client = request.getfixturevalue("dynamo") if store_kind == "dynamodb" else None
    p, racing = _race_platform(_store(store_kind, client))
    floor = p.setup.config.reserve_floor
    seen: dict[str, Any] = {}

    def run_a() -> None:
        if a_finishes:
            seen["a"] = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
        else:  # A's hold lands and its transfer moves money, nothing else yet
            ctx = p.step_context(
                "bridge_shortfall",
                {"obligation_ref": "ob-a1"},
                tenant="t-alpha",
                proposal_id="run-a",
            )
            (op,) = p.specs["bridge_shortfall"].steps[0].build(ctx)
            seen["a"] = p.fake.execute(op)

    # B passes its policy and rule-1 recheck, reads the wallet in its build, and then A runs
    # before B's hold write lands.
    racing.interleave = run_a
    b = p.run("bridge_shortfall", {"obligation_ref": "ob-b1"}, tenant="t-bravo")

    assert "a" in seen
    transfers = p.movements("connected_account_transfers")
    assert len(transfers) == 1  # only A moved money
    assert b.state == "FAILED"
    assert "below the floor" in (b.detail or "") or "below the reserve floor" in (b.detail or "")
    assert len(p.sent("connected_account_transfers.create")) == 1
    assert p.wallet(None) >= floor
    assert p.book.ledger.cash - p.book.ledger.held >= floor
    assert {h.amount for h in p.book.ledger.holds()} <= {usd("80")}


def test_a_lone_bridge_still_fits_the_wallet_room(store_kind, request):
    client = request.getfixturevalue("dynamo") if store_kind == "dynamodb" else None
    p, _ = _race_platform(_store(store_kind, client))
    out = p.run("bridge_shortfall", {"obligation_ref": "ob-a1"}, tenant="t-alpha")
    assert out.state == "SUCCEEDED", out.detail
    assert p.wallet(None) == usd("620")
