"""Rebuild awx_common dataclasses from awx_common.transport payloads.

transport.encode turns a dataclass into a plain dict and decode gives the dict back, so every
type that crosses Step Functions, Lambda or DynamoDB needs one explicit constructor here.
Unknown keys are refused so a payload cannot smuggle a field the code would then ignore.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from awx_common.approvals import ApprovalDecision, Decision, Origin, Proposal, RiskTier
from awx_common.ops import Operation, OperationResult, OperationStatus, ReadRequest
from awx_common.tenancy import TenantContext
from awx_common.workflow import PolicyDecision, PostconditionResult


def _only(data: Mapping[str, Any], allowed: set[str], what: str) -> None:
    if not isinstance(data, Mapping):
        raise TypeError(f"{what} must be an object")
    extra = set(data) - allowed
    if extra:
        raise ValueError(f"{what} has unexpected fields {sorted(extra)}")


def operation_from(data: Mapping[str, Any]) -> Operation:
    _only(
        data,
        {
            "kit",
            "name",
            "method",
            "path",
            "body",
            "request_id",
            "business_key",
            "intent_hash",
            "on_behalf_of",
        },
        "operation",
    )
    return Operation(**data)


def result_from(data: Mapping[str, Any]) -> OperationResult:
    _only(
        data,
        {
            "request_id",
            "status",
            "http_status",
            "provider_id",
            "provider_status",
            "body",
            "error_type",
            "error_code",
            "error_message",
        },
        "operation result",
    )
    return OperationResult(**{**data, "status": OperationStatus(data["status"])})


def read_request_from(data: Mapping[str, Any]) -> ReadRequest:
    _only(data, {"path", "query", "on_behalf_of"}, "read request")
    return ReadRequest(
        path=data["path"],
        query={str(k): str(v) for k, v in (data.get("query") or {}).items()},
        on_behalf_of=data.get("on_behalf_of"),
    )


def proposal_from(data: Mapping[str, Any]) -> Proposal:
    _only(
        data,
        {
            "proposal_id",
            "kit",
            "action",
            "intent",
            "computed",
            "intent_hash",
            "risk_tier",
            "reasons",
            "proposed_by",
            "created_at",
            "expires_at",
            "origin",
        },
        "proposal",
    )
    return Proposal(
        **{
            **data,
            "risk_tier": RiskTier(data["risk_tier"]),
            "reasons": tuple(data["reasons"]),
            "computed": dict(data.get("computed") or {}),
            "origin": Origin(data.get("origin") or Origin.AGENT.value),
        }
    )


def policy_decision_from(data: Mapping[str, Any]) -> PolicyDecision:
    _only(data, {"risk_tier", "reasons", "computed"}, "policy decision")
    return PolicyDecision(
        risk_tier=RiskTier(data["risk_tier"]),
        reasons=tuple(data["reasons"]),
        computed=dict(data.get("computed") or {}),
    )


def approval_decision_from(data: Mapping[str, Any]) -> ApprovalDecision:
    _only(data, {"proposal_id", "intent_hash", "approver", "decision", "decided_at"}, "decision")
    return ApprovalDecision(**{**data, "decision": Decision(data["decision"])})


def tenant_from(data: Mapping[str, Any]) -> TenantContext:
    _only(data, {"tenant_id", "connected_account_id", "kit", "role"}, "tenant")
    return TenantContext(**data)


def postcondition_from(data: Mapping[str, Any] | None) -> PostconditionResult | None:
    if data is None:
        return None
    _only(data, {"ok", "detail"}, "postcondition")
    return PostconditionResult(ok=bool(data["ok"]), detail=str(data["detail"]))


# An AwxError crossing the reader Lambda keeps its fields: the Lambda error payload carries
# only errorType and errorMessage, so the reader appends them to the message after this mark.
ERROR_MARK = " [awx-error "
ERROR_FIELDS_MAX = 1024  # bytes of JSON appended; the reader logs the whole message


def with_error_fields(exc: Exception) -> None:
    """Append code, http_status and details to an AwxError's message, in place (reader)."""
    fields = {
        "code": getattr(exc, "code", None),
        "http_status": getattr(exc, "http_status", None),
        "details": getattr(exc, "details", None) or {},
    }
    raw = json.dumps(fields, default=str)
    if len(raw.encode()) > ERROR_FIELDS_MAX:  # keep code and http_status; drop the details
        fields["details"] = {"truncated": True}
        raw = json.dumps(fields, default=str)
        if len(raw.encode()) > ERROR_FIELDS_MAX:
            fields["code"] = None
            raw = json.dumps(fields, default=str)
    exc.args = (f"{exc}{ERROR_MARK}{raw}]",)


def error_fields(message: str) -> tuple[str, dict[str, Any]]:
    """Split a reader error message into its text and the fields with_error_fields kept."""
    head, mark, tail = message.rpartition(ERROR_MARK)
    if not mark or not tail.endswith("]"):
        return message, {}
    try:
        raw = json.loads(tail[:-1])
    except ValueError:
        return message, {}
    if not isinstance(raw, dict):
        return message, {}
    code, status, details = raw.get("code"), raw.get("http_status"), raw.get("details")
    return head, {
        "code": code if isinstance(code, str) else None,
        "http_status": status if isinstance(status, int) else None,
        "details": details if isinstance(details, dict) else {},
    }
