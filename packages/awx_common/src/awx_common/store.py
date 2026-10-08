"""Kit-owned state (ledgers, locks, evidence indexes) behind one small port.

A KitStore instance is already scoped to one kit and one tenant, so keys are kit-local.
Writes use optimistic concurrency: expected_version=None creates a key that must not exist;
an int must equal the stored version. Values must survive awx_common.transport, so amounts
stay exact. awx_spine_aws implements this on the DynamoDB ledger table; tests use InMemoryKitStore.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from . import transport


class VersionConflict(Exception):
    """The key exists when it should not, or its version moved."""


@dataclass(frozen=True)
class StoredItem:
    key: str
    value: Mapping[str, Any]
    version: int


@runtime_checkable
class KitStore(Protocol):
    def get(self, key: str) -> StoredItem | None: ...

    def put(
        self, key: str, value: Mapping[str, Any], *, expected_version: int | None
    ) -> StoredItem: ...

    def query(self, prefix: str) -> Sequence[StoredItem]: ...


class InMemoryKitStore:
    """Test store. Round-trips every value through transport to catch non-portable values."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, int]] = {}

    def get(self, key: str) -> StoredItem | None:
        found = self._items.get(key)
        if found is None:
            return None
        text, version = found
        return StoredItem(key, transport.loads(text), version)

    def put(
        self, key: str, value: Mapping[str, Any], *, expected_version: int | None
    ) -> StoredItem:
        if not key:
            raise ValueError("key is required")
        current = self._items.get(key)
        if expected_version is None and current is not None:
            raise VersionConflict(f"{key} already exists")
        if expected_version is not None and (current is None or current[1] != expected_version):
            raise VersionConflict(f"{key} is not at version {expected_version}")
        version = 1 if current is None else current[1] + 1
        self._items[key] = (transport.dumps(value), version)
        return StoredItem(key, transport.loads(self._items[key][0]), version)

    def query(self, prefix: str) -> Sequence[StoredItem]:
        return [
            StoredItem(k, transport.loads(text), version)
            for k, (text, version) in sorted(self._items.items())
            if k.startswith(prefix)
        ]
