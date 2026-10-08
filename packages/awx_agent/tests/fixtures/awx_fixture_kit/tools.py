from awx_common.tools import ToolKind, ToolSpec

_PAY = {
    "type": "object",
    "properties": {
        "amount": {"type": "string", "description": "Decimal amount", "pattern": r"^\d+\.\d{2}$"},
        "currency": {"type": "string", "enum": ["AUD", "USD"]},
        "beneficiary_name": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    "required": ["amount", "currency", "beneficiary_name"],
    "additionalProperties": False,
}

TOOLS = (
    ToolSpec(
        name="read_balances",
        kind=ToolKind.READ,
        description="Current wallet balances.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        name="propose_pay_supplier",
        kind=ToolKind.PROPOSE,
        description="Propose paying a supplier from the wallet.",
        input_schema=_PAY,
    ),
    ToolSpec(
        name="propose_probe_write",
        kind=ToolKind.PROPOSE,
        description="Fixture only: a policy that (wrongly) writes the kit store.",
        input_schema=_PAY,
    ),
)
