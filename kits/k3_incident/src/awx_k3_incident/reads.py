"""Kit 3 read tools. Pure: each uses ctx.read only and returns plain JSON-able data.

The model reads these to summarise and recommend. Nothing it returns authorises a write: the
propose policy recomputes everything from fresh reads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from awx_common.workflow import PolicyContext

from . import airwallex as awx
from .closure import CLOSURE_PREFIX
from .finality import failure_code_of, failure_of, failure_type_of
from .intent import validate_transfer_ref
from .policy import evaluate_incident

_FIELDS = (
    "status",
    "transfer_amount",
    "transfer_currency",
    "transfer_method",
    "beneficiary_id",
    "reference",
    "created_at",
    "last_updated_at",
)


def read_transfer(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    tid = validate_transfer_ref(raw)["transfer_id"]
    t = awx.get_transfer(ctx.read, tid)
    out: dict[str, Any] = {"id": tid, **{k: t.get(k) for k in _FIELDS}}
    failure = failure_of(t)
    out["failure"] = dict(failure) if failure else None
    out["failure_code"] = failure_code_of(t)[0]
    out["failure_type"] = failure_type_of(t)[0]  # documented name for the code, if known
    return out


def read_incident_assessment(raw: Mapping[str, Any], ctx: PolicyContext) -> Mapping[str, Any]:
    tid = validate_transfer_ref(raw)["transfer_id"]
    ev = evaluate_incident(ctx.read, tid, ctx.store, ctx.now)
    plan = ev.plan.computed() if ev.plan else None
    return {
        "transfer_id": tid,
        "status": ev.assessment.status,
        "failure_code": ev.assessment.failure_code,
        "failure_type": ev.assessment.failure_type,
        "failure": dict(ev.assessment.failure) if ev.assessment.failure else None,
        "decision": ev.decision.value,
        "reasons": list(ev.reasons),
        "duplicate_lock_held": ev.lock_held,
        "existing_replacements": list(ev.existing_replacements),
        "replacement_plan": plan,
        "closure": _closure(ctx, tid),
    }


def _closure(ctx: PolicyContext, tid: str) -> Mapping[str, Any] | None:
    if ctx.store is None:
        return None
    item = ctx.store.get(f"{CLOSURE_PREFIX}{tid}")
    return dict(item.value) if item else None


READS: dict[str, Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]] = {
    "read_transfer": read_transfer,
    "read_incident_assessment": read_incident_assessment,
}
