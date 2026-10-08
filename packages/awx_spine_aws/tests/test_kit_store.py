"""DynamoDbKitStore against moto: scoping, optimistic versions and exact values."""

from __future__ import annotations

import boto3
import pytest
from awx_common import Money
from awx_common.store import KitStore, VersionConflict
from awx_spine_aws.store import DynamoDbKitStore
from moto import mock_aws


@pytest.fixture
def db():
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


def store(db, kit="k5", tenant="tenant-a") -> DynamoDbKitStore:
    return DynamoDbKitStore("ledger", kit=kit, tenant_id=tenant, client=db)


def test_implements_the_protocol(db):
    assert isinstance(store(db), KitStore)


def test_create_then_update_with_versions(db):
    s = store(db)
    first = s.put("reserve", {"held": Money.of("10.05", "AUD")}, expected_version=None)
    assert first.version == 1
    with pytest.raises(VersionConflict, match="already exists"):
        s.put("reserve", {"held": Money.of("1.00", "AUD")}, expected_version=None)
    second = s.put("reserve", {"held": Money.of("12.00", "AUD")}, expected_version=1)
    assert second.version == 2
    with pytest.raises(VersionConflict, match="version 1"):
        s.put("reserve", {"held": Money.of("0.01", "AUD")}, expected_version=1)
    got = s.get("reserve")
    assert got.value == {"held": Money.of("12.00", "AUD")}  # exact through transport
    assert got.version == 2


def test_update_of_a_missing_key_conflicts(db):
    with pytest.raises(VersionConflict):
        store(db).put("nope", {"x": 1}, expected_version=3)


def test_query_by_prefix_is_sorted_and_scoped(db):
    s = store(db)
    for key in ("loan#2", "loan#1", "lock#1"):
        s.put(key, {"k": key}, expected_version=None)
    store(db, tenant="tenant-b").put("loan#9", {"k": "other"}, expected_version=None)
    store(db, kit="k6").put("loan#8", {"k": "other kit"}, expected_version=None)
    assert [i.key for i in s.query("loan#")] == ["loan#1", "loan#2"]
    assert [i.key for i in s.query("")] == ["loan#1", "loan#2", "lock#1"]
    assert s.get("loan#9") is None


def test_floats_are_refused(db):
    with pytest.raises(TypeError):
        store(db).put("x", {"amount": 1.5}, expected_version=None)


@pytest.mark.parametrize(("kit", "tenant"), [("k9", "t"), ("k5", ""), ("k5", "a#b")])
def test_bad_scope(db, kit, tenant):
    with pytest.raises(ValueError, match=r"kit|tenant_id"):
        DynamoDbKitStore("ledger", kit=kit, tenant_id=tenant, client=db)


def _race(make):
    """Two writers each read the key as absent, then both create it."""
    a, b = make(), make()
    assert a.get("lock") is None
    assert b.get("lock") is None
    outcomes = []
    for writer, owner in ((a, "run-a"), (b, "run-b")):
        try:
            writer.put("lock", {"owner": owner}, expected_version=None)
            outcomes.append(owner)
        except VersionConflict:
            outcomes.append("conflict")
    return outcomes, a.get("lock")


def test_two_racing_creates_exactly_one_wins_dynamodb(db):
    outcomes, item = _race(lambda: store(db))
    assert outcomes == ["run-a", "conflict"]
    assert item.value == {"owner": "run-a"}
    assert item.version == 1


def test_two_racing_creates_exactly_one_wins_threaded_dynamodb(db):
    import threading

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def create(owner):
        s = store(db)
        barrier.wait()
        try:
            s.put("lock", {"owner": owner}, expected_version=None)
            results[owner] = "won"
        except VersionConflict:
            results[owner] = "conflict"

    threads = [threading.Thread(target=create, args=(o,)) for o in ("run-a", "run-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["conflict", "won"]
    winner = next(o for o, r in results.items() if r == "won")
    assert store(db).get("lock").value == {"owner": winner}


def test_two_racing_creates_exactly_one_wins_in_memory():
    from awx_common.store import InMemoryKitStore

    shared = InMemoryKitStore()
    outcomes, item = _race(lambda: shared)
    assert outcomes == ["run-a", "conflict"]
    assert item.value == {"owner": "run-a"}


def test_two_racing_updates_from_one_version_exactly_one_wins_threaded_dynamodb(db):
    import threading

    store(db).put("pool", {"owner": "seed"}, expected_version=None)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def update(owner):
        s = store(db)
        barrier.wait()
        try:
            s.put("pool", {"owner": owner}, expected_version=1)
            results[owner] = "won"
        except VersionConflict:
            results[owner] = "conflict"

    threads = [threading.Thread(target=update, args=(o,)) for o in ("run-a", "run-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["conflict", "won"]
    winner = next(o for o, r in results.items() if r == "won")
    item = store(db).get("pool")
    assert item.version == 2
    assert item.value == {"owner": winner}


def test_two_racing_updates_from_one_version_exactly_one_wins_in_memory():
    from awx_common.store import InMemoryKitStore

    shared = InMemoryKitStore()
    shared.put("pool", {"owner": "seed"}, expected_version=None)
    seen = shared.get("pool").version  # both writers read version 1
    shared.put("pool", {"owner": "run-a"}, expected_version=seen)
    with pytest.raises(VersionConflict):
        shared.put("pool", {"owner": "run-b"}, expected_version=seen)
    item = shared.get("pool")
    assert item.version == 2
    assert item.value == {"owner": "run-a"}
