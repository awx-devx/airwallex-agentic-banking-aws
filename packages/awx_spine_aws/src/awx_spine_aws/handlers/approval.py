"""Approval API Lambda (HTTP API, Cognito JWT authorizer on the ACCESS token).

GET  /proposals[?status=S]               the caller's tenant's proposals, newest first
GET  /proposals/{proposal_id}            the exact canonical intent, computed amounts, hash,
                                         tier and state
POST /proposals/{proposal_id}/decision   {"decision": "APPROVE" | "REJECT", "intent_hash": h}

Identity is the verified token's `sub` (the same claim the propose tool records as
proposed_by). The tenant is the `tenant_id` claim that the Pre Token Generation V2 trigger
adds to the access token; groups come from `cognito:groups`. Reading needs the requester or
approver group; only the approver group may decide. The caller must echo the intent hash they
were shown. A decision is checked with approval_state against the proposal and every stored
decision first (wrong hash, self-approval or a late decision is refused with 409 and never
stored), then stored once per approver. When the proposal becomes APPROVED or REJECTED the
workflow's task token is sent; the state machine then re-checks the binding from the table
and re-runs policy before anything executes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from awx_common.approvals import (
    ApprovalBindingError,
    ApprovalDecision,
    ApprovalState,
    Decision,
    approval_state,
)
from awx_common.transport import dumps

from ..repos import DecisionRepo, DuplicateDecision, ProposalRepo, StoredProposal
from ._common import boto_client, env, log

APPROVER_GROUP = "approver"
REQUESTER_GROUP = "requester"
TENANT_CLAIM = "tenant_id"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_STATUS = re.compile(r"^[A-Z_]{1,32}$")


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": dumps(body),
    }


def _groups(raw: Any) -> set[str]:
    """API Gateway passes array claims as a JSON list or as the string "[a b]"."""
    if isinstance(raw, list):
        return {str(g) for g in raw}
    if not isinstance(raw, str) or not raw:
        return set()
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return {g for g in re.split(r"[\s,]+", text.replace('"', "")) if g}


def _claims(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})


def _proposals() -> ProposalRepo:
    return ProposalRepo(env("AWX_PROPOSALS_TABLE"), client=boto_client("dynamodb"))


def _decisions() -> DecisionRepo:
    return DecisionRepo(env("AWX_DECISIONS_TABLE"), client=boto_client("dynamodb"))


def _run_still_live(sfn: Any, proposal_id: str) -> bool:
    """True only when the execution waiting on this proposal is RUNNING and the proposal has
    no stored outcome. An unknown execution, or a failed describe, counts as gone."""
    stored = _proposals().get(proposal_id)
    if stored is None or stored.outcome or not stored.execution_arn:
        return False
    try:
        status = sfn.describe_execution(executionArn=stored.execution_arn)["status"]
    except Exception as exc:
        log("approval.describe_failed", proposal_id=proposal_id, error=type(exc).__name__)
        return False
    return status == "RUNNING"


def _status(stored: StoredProposal, decisions: Sequence[ApprovalDecision], now: datetime) -> str:
    if stored.outcome:
        return stored.outcome
    try:
        return approval_state(stored.proposal, decisions, now=now).value
    except ApprovalBindingError:
        return ApprovalState.REJECTED.value


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    claims = _claims(event)
    sub = claims.get("sub")
    tenant_id = claims.get(TENANT_CLAIM)
    if not sub or not tenant_id:
        return _response(401, {"error": "token lacks sub or tenant_id"})
    if claims.get("token_use") != "access":
        return _response(401, {"error": "an access token is required"})
    groups = _groups(claims.get("cognito:groups"))
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    route = event.get("routeKey") or ""
    proposal_id = (event.get("pathParameters") or {}).get("proposal_id")
    now = datetime.now(UTC)

    if method == "GET" and not groups & {APPROVER_GROUP, REQUESTER_GROUP}:
        return _response(403, {"error": "not a requester or approver"})
    if method == "GET" and not proposal_id:
        status = (event.get("queryStringParameters") or {}).get("status")
        if status is not None and not _STATUS.match(status):
            return _response(400, {"error": "bad status filter"})
        return _list(tenant_id, status, now)

    stored = _proposals().get(proposal_id) if proposal_id else None
    # The proposal's own tenant may read it; only its approver tenant may decide it.
    if stored is None or tenant_id not in (stored.tenant_id, stored.approver_tenant):
        return _response(404, {"error": "no such proposal"})  # never reveal other tenants
    if method == "GET":
        return _response(200, _view(stored, now))
    if method == "POST" and route.endswith("/decision"):
        if APPROVER_GROUP not in groups:
            return _response(403, {"error": "only approvers may decide"})
        if stored.approver_tenant is None:  # a kit 5-8 row with no approver: fail closed
            return _response(
                409,
                {"error": "this proposal predates approver routing; re-propose it to approve"},
            )
        if tenant_id != stored.approver_tenant:
            return _response(403, {"error": "this proposal is decided by another tenant"})
        return _decide(stored, sub, event.get("body"), now)
    return _response(405, {"error": "method not allowed"})


def _view(stored: StoredProposal, now: datetime) -> dict[str, Any]:
    p = stored.proposal
    decisions = _decisions().list(p.proposal_id)
    return {
        "proposal": p,
        "tenant_id": stored.tenant_id,
        "approver_tenant": stored.approver_tenant,
        "parent_proposal_id": stored.parent_proposal_id,
        "status": _status(stored, decisions, now),
        "decisions": [
            {"approver": d.approver, "decision": d.decision.value, "decided_at": d.decided_at}
            for d in decisions
        ],
    }


def _list(tenant_id: str, status: str | None, now: datetime) -> dict[str, Any]:
    items = []
    seen: set[str] = set()
    repo = _proposals()
    # The caller's own proposals (to read their status) and those it approves.
    for stored in [*repo.list_for_approver(tenant_id), *repo.list_for_tenant(tenant_id)]:
        if stored.proposal.proposal_id in seen:
            continue
        seen.add(stored.proposal.proposal_id)
        view = _view(stored, now)
        if status is None or view["status"] == status:
            items.append(view)
    return _response(200, {"proposals": items})


def _decide(stored: StoredProposal, approver: str, raw_body: Any, now: datetime) -> dict[str, Any]:
    p = stored.proposal
    try:
        body = json.loads(raw_body or "{}")
        decision = Decision(body["decision"])
        shown_hash = body["intent_hash"]
    except (ValueError, KeyError, TypeError):
        return _response(400, {"error": "need decision APPROVE|REJECT and intent_hash"})
    if not isinstance(shown_hash, str) or not _HASH.match(shown_hash):
        return _response(400, {"error": "intent_hash must be a SHA-256 hex digest"})
    existing = list(_decisions().list(p.proposal_id))
    try:
        before = approval_state(p, existing, now=now)
        if before is not ApprovalState.PENDING or stored.outcome:
            return _response(409, {"error": f"proposal is {stored.outcome or before.value}"})
        new = ApprovalDecision(p.proposal_id, shown_hash, approver, decision, now)
        approval_state(p, [*existing, new], now=now)  # refuses a non-binding decision
    except ApprovalBindingError as exc:
        log("approval.refused", proposal_id=p.proposal_id, reason=str(exc))
        return _response(409, {"error": str(exc)})
    if stored.task_token is None:
        return _response(409, {"error": "workflow is not waiting yet; retry shortly"})
    try:
        _decisions().put(new)
    except DuplicateDecision:
        return _response(409, {"error": "you have already decided on this proposal"})
    log("approval.decision", proposal_id=p.proposal_id, decision=decision.value)
    return _send_and_report(stored, now)


def _send_and_report(stored: StoredProposal, now: datetime) -> dict[str, Any]:
    """After a decision is stored: re-read every decision, and if the proposal is now decided
    send the workflow's task token and report the result."""
    p = stored.proposal
    # Two approvers deciding at once each see one decision; re-read after the write so the
    # second of them sends the token.
    try:
        after = approval_state(p, _decisions().list(p.proposal_id), now=now)
    except ApprovalBindingError as exc:
        return _response(409, {"error": str(exc)})
    log("approval.state", proposal_id=p.proposal_id, state=after.value)
    if after in (ApprovalState.APPROVED, ApprovalState.REJECTED):
        sfn = boto_client("stepfunctions")
        try:
            sfn.send_task_success(
                taskToken=stored.task_token, output=json.dumps({"approval": after.value})
            )
        except (sfn.exceptions.TaskTimedOut, sfn.exceptions.InvalidToken):
            # The send failed. Decide from the run itself, not from the decisions (which the
            # caller just wrote): the other approver's send won only if the execution is still
            # RUNNING and has no terminal outcome. A wait that timed out, or an execution that
            # was stopped or failed, will never run the payment.
            if _run_still_live(sfn, p.proposal_id):
                return _response(200, {"status": after.value})
            return _response(410, {"error": "the approval window has closed"})
    return _response(200 if after is not ApprovalState.PENDING else 202, {"status": after.value})
