"""InMemoryKitStore: optimistic versions, prefix queries and transport round trips."""

import pytest
from awx_common import Money
from awx_common.store import InMemoryKitStore, KitStore, VersionConflict


def test_in_memory_store_versions_and_round_trips():
    store = InMemoryKitStore()
    assert isinstance(store, KitStore)
    first = store.put("loan/L1", {"principal": Money.of("5000", "AUD")}, expected_version=None)
    assert first.version == 1
    assert store.get("loan/L1").value["principal"] == Money.of("5000.00", "AUD")
    with pytest.raises(VersionConflict):
        store.put("loan/L1", {"x": 1}, expected_version=None)
    with pytest.raises(VersionConflict):
        store.put("loan/L1", {"x": 1}, expected_version=2)
    assert store.put("loan/L1", {"x": 1}, expected_version=1).version == 2
    store.put("loan/L2", {"x": 2}, expected_version=None)
    store.put("lock/L1", {"x": 3}, expected_version=None)
    assert [i.key for i in store.query("loan/")] == ["loan/L1", "loan/L2"]
    assert store.get("missing") is None
    with pytest.raises(TypeError):
        store.put("bad", {"amount": 1.5}, expected_version=None)
