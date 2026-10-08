"""Sandbox-only finality drivers (runner rule 10).

This is the kit-facing home of the driver: kits depend on awx_common only, so they import it
from here. awx_spine.sandbox re-exports the same objects.

Airwallex sandbox transfers do not move past PROCESSING without a simulation call
(awx_spine FAKE-FIDELITY.md), so a finality poll on a kit's transfer would time out.
make_transfer_advance returns a FinalitySpec.sandbox_advance callable: on each non-terminal
poll the runner gives it the latest transfer body and it returns the simulation Operation that
moves the transfer one step, or None to wait.

- SCHEDULED or PROCESSING -> SENT.
- SENT -> PAID once the transfer has been SENT for hold_s seconds (judged from the body's
  updated_at, which the sandbox sets at each transition), unless the transfer's reference
  carries nosettle_marker: then it stays SENT, so kit 3 can stage a failure.
- Terminal (PAID, CANCELLED) or unknown statuses -> None.

Each simulation Operation gets a deterministic request_id from (kit, transfer id, target
status, attempt), so a runner retry builds the same Operation and the operation store refuses
to send it twice. The simulation endpoint ignores request_id, so the body carries none
(the spine sends bodies exactly as built). on_behalf_of is copied from the original
operation: a connected account's transfer can only be simulated with its header.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from awx_common import Operation, OperationResult, derive_request_id
from awx_common.workflow import SandboxAdvance

__all__ = ["IN_FLIGHT", "TRANSITION_OP", "make_transfer_advance", "transition_operation"]

TRANSITION_OP = "transfers.simulate_transition"
IN_FLIGHT = frozenset({"SCHEDULED", "PROCESSING", "OVERDUE"})


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) > 5 and value[-5] in "+-" and value[-4:].isdigit():
        value = value[:-2] + ":" + value[-2:]
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def transition_operation(
    kit: str, op: Operation, transfer_id: str, target: str, attempt: int
) -> Operation:
    """The simulation Operation moving transfer_id to target, deterministic per attempt."""
    business_key = f"sandbox-advance:{transfer_id}"
    return Operation(
        kit=kit,
        name=TRANSITION_OP,
        method="POST",
        path=f"/api/v1/simulation/transfers/{transfer_id}/transition",
        body={"next_status": target},
        request_id=derive_request_id(kit, business_key, f"{target}:{attempt}"),
        business_key=business_key,
        intent_hash=op.intent_hash,
        on_behalf_of=op.on_behalf_of,
    )


def make_transfer_advance(
    kit: str, *, hold_s: int = 30, nosettle_marker: str = "-nosettle"
) -> SandboxAdvance:
    if hold_s < 0:
        raise ValueError("hold_s must not be negative")
    if not nosettle_marker:
        raise ValueError("nosettle_marker must not be empty")

    def advance(
        op: Operation,
        result: OperationResult,
        body: Mapping[str, Any],
        now: datetime,
        attempt: int,
    ) -> Operation | None:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        transfer_id = body.get("id") or result.provider_id
        if not isinstance(transfer_id, str) or not transfer_id:
            return None
        status = body.get("status")
        if status in IN_FLIGHT:
            return transition_operation(kit, op, transfer_id, "SENT", attempt)
        if status != "SENT":
            return None
        reference = body.get("reference", op.body.get("reference"))
        if isinstance(reference, str) and nosettle_marker in reference:
            return None
        since = _parse(body.get("updated_at"))
        if since is not None and (now - since).total_seconds() < hold_s:
            return None
        return transition_operation(kit, op, transfer_id, "PAID", attempt)

    return advance
