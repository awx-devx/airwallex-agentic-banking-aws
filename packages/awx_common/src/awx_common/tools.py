"""Agent tool specs. Kits declare their tools as data; the agent plane registers them.

READ tools read and explain. PROPOSE tools start a deterministic workflow that may need
approval. No tool input may carry a server-owned field: the model never picks a connected
account, tenant, request_id or intent hash.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

SERVER_OWNED_FIELDS = frozenset(
    {
        "on_behalf_of",
        "x-on-behalf-of",
        "x_on_behalf_of",
        "connected_account_id",
        "account_id",
        "tenant_id",
        "request_id",
        "intent_hash",
        "proposal_id_override",
        "approver",
    }
)
_NAME = re.compile(r"^(read|propose)_[a-z0-9_]{1,57}$")


class ToolKind(StrEnum):
    READ = "READ"
    PROPOSE = "PROPOSE"


def _property_names(schema: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for key, sub in (schema.get("properties") or {}).items():
        names.add(key)
        if isinstance(sub, Mapping):
            names |= _property_names(sub)
            items = sub.get("items")
            if isinstance(items, Mapping):
                names |= _property_names(items)
    return names


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: ToolKind
    description: str
    input_schema: Mapping[str, Any]  # JSON Schema object with additionalProperties false

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"tool name {self.name!r} must be read_* or propose_*, snake_case")
        prefix = self.name.split("_", 1)[0]
        if prefix.upper() != self.kind.value:
            raise ValueError(f"tool {self.name!r} prefix does not match kind {self.kind.value}")
        if self.input_schema.get("type") != "object":
            raise ValueError("input_schema must be a JSON Schema object")
        if self.input_schema.get("additionalProperties") is not False:
            raise ValueError("input_schema must set additionalProperties to false")
        owned = _property_names(self.input_schema) & SERVER_OWNED_FIELDS
        if owned:
            raise ValueError(f"tool {self.name!r} exposes server-owned fields {sorted(owned)}")
        if not self.description.strip():
            raise ValueError("tool description is required")
