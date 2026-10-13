"""Tool schemas: kit JSON Schema -> AgentCore Gateway tool definition, plus argument checks.

The Gateway Lambda-target schema accepts only type, description, properties, required and items
(AgentCore Developer Guide, "Lambda function tool schema"). Kit schemas may use more JSON Schema
keywords (enum, pattern, format, minLength ...). Those are folded into the description so the
model still sees them, and the tool handler enforces the shape itself: the kit's validate()
is the real check, check_arguments() is the cheap structural one in front of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common.tools import SERVER_OWNED_FIELDS, ToolSpec

from .identity import IDENTITY_FIELD

_GATEWAY_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})
_KEPT = frozenset({"type", "description", "properties", "required", "items"})
_DROPPED_SILENTLY = frozenset({"additionalProperties", "$schema", "title", "default"})


class ToolInputError(ValueError):
    """The model's tool arguments do not fit the tool. Safe to show to the model."""


def _gateway_type(schema: Mapping[str, Any]) -> str:
    kind = schema.get("type")
    if isinstance(kind, list):
        non_null = [k for k in kind if k != "null"]
        kind = non_null[0] if len(non_null) == 1 else None
    if kind not in _GATEWAY_TYPES:
        raise ValueError(f"schema type {schema.get('type')!r} has no Gateway equivalent")
    return str(kind)


def _extra_constraints(schema: Mapping[str, Any]) -> str:
    notes = []
    for key in sorted(set(schema) - _KEPT - _DROPPED_SILENTLY):
        value = schema[key]
        if key == "enum":
            notes.append("one of " + ", ".join(str(v) for v in value))
        else:
            notes.append(f"{key}={value}")
    return "; ".join(notes)


def to_gateway_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one JSON Schema node into a Gateway SchemaDefinition (camelCase JSON)."""
    out: dict[str, Any] = {"type": _gateway_type(schema)}
    description = str(schema.get("description") or "").strip()
    extra = _extra_constraints(schema)
    if extra:
        description = f"{description} ({extra})" if description else f"Constraints: {extra}"
    if description:
        out["description"] = description
    if out["type"] == "object":
        props = schema.get("properties") or {}
        out["properties"] = {name: to_gateway_schema(sub) for name, sub in props.items()}
        if schema.get("required"):
            out["required"] = list(schema["required"])
    if out["type"] == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            raise ValueError("array schemas need an items schema")
        out["items"] = to_gateway_schema(items)
    return out


# Appended to every propose_* tool's description. Code sets the risk tier, which decides
# whether a different person must approve the proposal, so no description says in advance.
PROPOSE_SUFFIX = " Creates a proposal only. Code sets its risk tier when you propose."

IDENTITY_PROPERTY = {
    "type": "string",
    "description": "Leave unset. The server fills this with the signed-in caller.",
}


def tool_definition(spec: ToolSpec) -> dict[str, Any]:
    """A Gateway ToolDefinition for one kit tool.

    IDENTITY_FIELD is declared as an optional property so the value the interceptor writes is a
    documented inputSchema property when it reaches the Lambda (the Lambda input format is "a
    map of properties from the inputSchema"). Whatever the model puts there is overwritten by
    the interceptor and then signature-checked by the tool Lambda.
    """
    description = spec.description.strip()
    if spec.kind.value == "PROPOSE":
        description += PROPOSE_SUFFIX
    input_schema = to_gateway_schema(spec.input_schema)
    input_schema.setdefault("properties", {})[IDENTITY_FIELD] = dict(IDENTITY_PROPERTY)
    return {"name": spec.name, "description": description, "inputSchema": input_schema}


def _reject_floats(value: Any, path: str) -> None:
    if isinstance(value, float):
        raise ToolInputError(
            f"{path} is a JSON number with a fraction; send amounts as decimal strings like '12.34'"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _reject_floats(item, f"{path}[{i}]")


def check_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Structural check of model arguments before the kit's own validation.

    Refuses server-owned fields at any depth, unknown top-level fields (the kit schemas set
    additionalProperties false), missing required fields and floats anywhere.
    """
    if not isinstance(arguments, Mapping):
        raise ToolInputError("tool arguments must be a JSON object")
    args = {k: v for k, v in arguments.items() if k != IDENTITY_FIELD}
    owned = _keys_deep(args) & (SERVER_OWNED_FIELDS | {IDENTITY_FIELD})
    if owned:
        raise ToolInputError(f"fields {sorted(owned)} are set by the server, not by the model")
    allowed = set((spec.input_schema.get("properties") or {}).keys())
    unknown = set(args) - allowed
    if unknown:
        raise ToolInputError(f"unknown fields {sorted(unknown)} for {spec.name}")
    missing = set(spec.input_schema.get("required") or ()) - set(args)
    if missing:
        raise ToolInputError(f"missing required fields {sorted(missing)} for {spec.name}")
    _reject_floats(args, "input")
    return args


def _keys_deep(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.add(str(key))
            keys |= _keys_deep(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _keys_deep(item)
    return keys
