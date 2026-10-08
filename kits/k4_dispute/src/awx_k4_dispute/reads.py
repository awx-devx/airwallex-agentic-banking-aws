"""READS: pure read tools. Each uses ctx.read only and returns plain JSON for the model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from awx_common import to_jsonable
from awx_common.workflow import PolicyContext

from .cases import attempt_state, attempts, case_value
from .config import DEFAULT_CONFIG, DisputeConfig, StrengthBand
from .deadlines import ActionKind, deadline_for
from .economics import UnsupportedCurrency, compute_economics
from .evidence import EvidenceFile, Fact, resolve_evidence
from .intents import IntentError, _dispute_id, _keys, validate_read_economics
from .policy import case_economics, existing_refunds, read_view
from .provider import (
    CHALLENGE_REASON_FAMILIES,
    DisputeView,
    disputes_list_read,
    items_of,
    parse_dispute,
)
from .tools import QUEUE_STATUSES


def _view_json(view: DisputeView, config: DisputeConfig, ctx: PolicyContext) -> dict[str, Any]:
    accept = deadline_for(view, ActionKind.ACCEPT, config, ctx.now)
    challenge = deadline_for(view, ActionKind.CHALLENGE, config, ctx.now)
    return {
        "dispute_id": view.dispute_id,
        "payment_intent_id": view.payment_intent_id,
        "disputed_amount": view.amount,
        "reason_code": view.reason_code,
        "reason_family": view.reason_family.value,
        "stage": view.stage,
        "status": view.raw_status,
        "respondable": view.is_respondable,
        "terminal": view.is_terminal,
        "respond_by": accept.respond_by,
        "deadline_source": accept.source.value,
        "accept_act_by": accept.act_by,
        "challenge_act_by": challenge.act_by,
        "challenge_window_open": not challenge.passed,
        "prior_challenge_seen": view.prior_challenge_seen,
        "challenge_reasons_that_fit": sorted(
            r.value for r, fams in CHALLENGE_REASON_FAMILIES.items() if view.reason_family in fams
        ),
    }


def read_dispute(
    raw: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig = DEFAULT_CONFIG
) -> Mapping[str, Any]:
    _keys(raw, {"dispute_id"}, set(), "input")
    view = read_view(_dispute_id(raw), ctx)
    out = _view_json(view, config, ctx)
    case = case_value(ctx.store, view.dispute_id)
    out["case"] = {
        "challenge_attempted": bool(attempts(case, "challenge")),
        "accept_attempted": bool(attempts(case, "accept")),
        "attempts": [
            {"action": a["action"], "state": attempt_state(case, a, view).value}
            for a in attempts(case)
        ],
        "attempt_refs": [
            {
                "action": a["action"],
                "stage": a["decision"]["computed"].get("stage"),
                "request_ref": a["request_id"],
                "state": attempt_state(case, a, view).value,
            }
            for a in attempts(case)
        ],
        "locked": bool(case.get("lock")),
        "referrals": len(case.get("referrals", [])),
        "outcome": (case.get("outcome") or {}).get("close_reason"),
    }
    return to_jsonable(out)


def read_dispute_queue(
    raw: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig = DEFAULT_CONFIG
) -> Mapping[str, Any]:
    _keys(raw, set(), {"status"}, "input")
    status = raw.get("status")
    if status is not None and status not in QUEUE_STATUSES:
        raise IntentError("status is not a dispute status")
    items = items_of(ctx.read(disputes_list_read(status)))
    rows = []
    for item in items:
        view = parse_dispute(item)
        row = _view_json(view, config, ctx)
        try:
            econ = compute_economics(view.amount, StrengthBand.NONE, config)
            row["contest_cannot_pay"] = not econ.contest_can_beat_accept
            row["cost_accept"] = econ.cost_accept
            row["cost_contest_best"] = econ.cost_contest_best
        except UnsupportedCurrency:
            row["contest_cannot_pay"] = None
            row["economics"] = "no economics configured for this currency"
        rows.append(row)
    far = "9999-12-31T23:59:59Z"
    rows.sort(key=lambda r: (to_jsonable(r["accept_act_by"]) or far, r["dispute_id"]))
    return to_jsonable({"disputes": rows, "count": len(rows)})


def read_dispute_economics(
    raw: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig = DEFAULT_CONFIG
) -> Mapping[str, Any]:
    parsed = validate_read_economics(raw, config)
    view = read_view(parsed["dispute_id"], ctx)
    indexed, problems = resolve_evidence(ctx.store, view.dispute_id, parsed["evidence_ids"], config)
    evidence: list[EvidenceFile] = [e.file for e in indexed]
    texts = {e.file.evidence_id: e.text for e in indexed if e.text}
    facts: list[Fact] = parsed["facts"]
    out = _view_json(view, config, ctx)
    out["evidence_problems"] = problems
    out["flagged_evidence"] = [e.file.evidence_id for e in indexed if e.markers]
    try:
        check, assessment, econ = case_economics(view, evidence, facts, config, texts)
    except UnsupportedCurrency as exc:
        out["economics"] = str(exc)
        return to_jsonable(out)
    out.update(econ.to_computed())
    out["economics_reasons"] = list(econ.reasons)
    out["evidence_reasons"] = list(assessment.reasons)
    out["verified_facts"] = len(check.verified)
    out["rejected_facts"] = [
        {"kind": f.kind.value, "evidence_id": f.evidence_id, "why": why}
        for f, why in check.rejected
    ]
    out["prior_undisputed_orders"] = assessment.prior_orders
    out["support_contacts"] = assessment.support_contacts
    return to_jsonable(out)


def read_refunds(
    raw: Mapping[str, Any], ctx: PolicyContext, config: DisputeConfig = DEFAULT_CONFIG
) -> Mapping[str, Any]:
    _keys(raw, {"dispute_id"}, set(), "input")
    view = read_view(_dispute_id(raw), ctx)
    refunds = existing_refunds(view, ctx)
    return to_jsonable(
        {
            "dispute_id": view.dispute_id,
            "payment_intent_id": view.payment_intent_id,
            "refunds": [
                {
                    "id": r.get("id"),
                    "status": r.get("status"),
                    "amount": r.get("amount"),
                    "currency": r.get("currency"),
                }
                for r in refunds
            ],
            "count": len(refunds),
        }
    )


READS: dict[str, Callable[[Mapping[str, Any], PolicyContext], Mapping[str, Any]]] = {
    "read_dispute_queue": read_dispute_queue,
    "read_dispute": read_dispute,
    "read_dispute_economics": read_dispute_economics,
    "read_refunds": read_refunds,
}
