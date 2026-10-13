"""Kit 3 agent tools. No input carries an amount, account or request_id.

The demo's original supplier payment (send_supplier_transfer) is deliberately NOT an agent tool:
an agent that reads supplier messages must not be able to send any amount to any beneficiary.
It is an operator staging path, workflow.STAGING_SPECS."""

from __future__ import annotations

from awx_common import ToolKind, ToolSpec

from .finality import SIMULATION_FAILURE_TYPES
from .intent import ID_PATTERN, KEY_PATTERN

_TRANSFER_ID = {"type": "string", "pattern": ID_PATTERN, "description": "Airwallex transfer id"}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_transfer",
        kind=ToolKind.READ,
        description="Read one transfer's status, amount and any failure (code and message).",
        input_schema={
            "type": "object",
            "properties": {"transfer_id": _TRANSFER_ID},
            "required": ["transfer_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="read_incident_assessment",
        kind=ToolKind.READ,
        description=(
            "Code's WAIT, REPLACE or ESCALATE decision for a transfer a supplier says did not "
            "arrive, with reasons, the duplicate-lock state and the replacement code would send."
        ),
        input_schema={
            "type": "object",
            "properties": {"transfer_id": _TRANSFER_ID},
            "required": ["transfer_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="propose_replace_failed_transfer",
        kind=ToolKind.PROPOSE,
        description=(
            "Propose the one replacement for a transfer Airwallex reports CANCELLED with a "
            "replaceable failure_type. Code copies amount and beneficiary from the original and "
            "refuses if the original is in flight or already replaced."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "original_transfer_id": _TRANSFER_ID,
                "incident_ref": {"type": "string", "pattern": KEY_PATTERN},
            },
            "required": ["original_transfer_id", "incident_ref"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="propose_close_incident",
        kind=ToolKind.PROPOSE,
        description=(
            "Record an incident's closure. Code refuses it unless the original is CANCELLED "
            "with a failure code and its one replacement is PAID or CANCELLED. Moves no money."
        ),
        input_schema={
            "type": "object",
            "properties": {"original_transfer_id": _TRANSFER_ID},
            "required": ["original_transfer_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="propose_simulate_transfer_failure",
        kind=ToolKind.PROPOSE,
        description=(
            "Sandbox only: move a kit-3 transfer to SENT, then FAILED with a failure_type, so it "
            "ends CANCELLED."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "transfer_id": _TRANSFER_ID,
                "failure_type": {"type": "string", "enum": sorted(SIMULATION_FAILURE_TYPES)},
            },
            "required": ["transfer_id", "failure_type"],
            "additionalProperties": False,
        },
    ),
)
