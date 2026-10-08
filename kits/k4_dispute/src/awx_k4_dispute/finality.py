"""Finality and reconciliation for dispute writes.

After an OUTCOME_UNKNOWN (runner rule 9) the executor calls the step's `reconcile` hook,
`dispute_reconcile`, which reads the dispute: if the provider shows the action, it landed and
the dispute is returned as the operation's object; if the dispute still waits for a response,
it did not land (None); anything else raises ReconcileUndetermined. awx_spine's endpoint table
does not count the dispute endpoints as deduplicating, so on None the executor still does NOT
resend: the run stops at OUTCOME_UNKNOWN, the postcondition keeps the dispute lock (rule M1)
and a person decides before the response deadline. The upload step has no hook: files.upload
is resend_safe, so the executor's generic rule applies.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from awx_common import Operation, OperationResult, ReadRequest
from awx_common.workflow import FinalitySpec, ReconcileUndetermined

from .cases import ANSWERED
from .provider import (
    ACCEPT_OP,
    CHALLENGE_OP,
    DISPUTES_PATH,
    DisputeStatus,
    DisputeView,
    ProviderShapeError,
    dispute_read,
    parse_dispute,
)

ANSWERED_BY_ACCEPT = ANSWERED["accept"]
ANSWERED_BY_CHALLENGE = ANSWERED["challenge"]


class Reconciliation(StrEnum):
    LANDED = "LANDED"
    NOT_LANDED = "NOT_LANDED"  # resend the same Operation (same request_id, same body)
    UNDETERMINED = "UNDETERMINED"  # stop; a person reads the case


def reconcile(op: Operation, view: DisputeView) -> Reconciliation:
    if op.name == ACCEPT_OP:
        answered = ANSWERED_BY_ACCEPT
    elif op.name == CHALLENGE_OP:
        answered = ANSWERED_BY_CHALLENGE
    else:
        raise ValueError(f"kit 4 does not reconcile {op.name}")
    action = "accept" if op.name == ACCEPT_OP else "challenge"
    recorded = any(a == action for a, _ in view.responses)
    if view.status in answered or recorded:
        return Reconciliation.LANDED  # kit 4 sends each action at most once per dispute
    if view.is_respondable and not view.prior_challenge_seen:
        return Reconciliation.NOT_LANDED
    return Reconciliation.UNDETERMINED


def dispute_id_of(op: Operation) -> str:
    """The dispute an accept or challenge Operation targets, from its path."""
    prefix = f"{DISPUTES_PATH}/"
    if not op.path.startswith(prefix):
        raise ValueError(f"{op.name} does not target a dispute")
    return op.path[len(prefix) :].split("/", 1)[0]


def dispute_reconcile(op: Operation, read) -> Mapping[str, Any] | None:
    """StepSpec.reconcile for accept and challenge (rule 9). Checks the object it finds is
    the operation's own dispute before treating it as the result."""
    dispute_id = dispute_id_of(op)
    body = read(dispute_read(dispute_id))
    view = parse_dispute(body)
    if view.dispute_id != dispute_id:
        raise ReconcileUndetermined(f"read-back returned {view.dispute_id}, not {dispute_id}")
    verdict = reconcile(op, view)
    if verdict is Reconciliation.LANDED:
        return body
    if verdict is Reconciliation.NOT_LANDED:
        return None
    raise ReconcileUndetermined(f"dispute {dispute_id} is {view.raw_status}; a person decides")


def dispute_id_from_result(result: OperationResult) -> str:
    if result.body and isinstance(result.body.get("id"), str) and result.body["id"]:
        return result.body["id"]
    if result.provider_id:
        return result.provider_id
    raise ProviderShapeError("write result carries no dispute id to read back")


def _status_of(body: Mapping[str, Any]) -> str:
    return parse_dispute(body).status.value


def _read(result: OperationResult) -> ReadRequest:
    return dispute_read(dispute_id_from_result(result))


ACCEPT_FINALITY = FinalitySpec(
    read_request=_read,
    status_of=_status_of,
    terminal=frozenset(
        s.value
        for s in (
            DisputeStatus.ACCEPTED,
            DisputeStatus.PENDING_CLOSURE,
            DisputeStatus.LOST,
            DisputeStatus.EXPIRED,
            DisputeStatus.WON,
            DisputeStatus.REVERSED,
        )
    ),
    success=frozenset(s.value for s in ANSWERED_BY_ACCEPT),
    interval_s=5,
    timeout_s=120,
)

# "Terminal" for the challenge STEP means the response was received, not that the case ended:
# the issuer's decision comes later (PENDING_DECISION, then WON, REVERSED or LOST; simulated by
# escalate and resolve in the sandbox).
CHALLENGE_FINALITY = FinalitySpec(
    read_request=_read,
    status_of=_status_of,
    terminal=frozenset(
        s.value
        for s in (
            *ANSWERED_BY_CHALLENGE,
            DisputeStatus.ACCEPTED,
            DisputeStatus.PENDING_CLOSURE,
            DisputeStatus.EXPIRED,
        )
    ),
    success=frozenset(s.value for s in ANSWERED_BY_CHALLENGE),
    interval_s=5,
    timeout_s=120,
)
