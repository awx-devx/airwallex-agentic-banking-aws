"""Shared contract for the Airwallex Agentic Banking on AWS reference kits.

Money, errors, operations, approvals, tenancy and tool specs.
"""

from .approvals import (
    ApprovalBindingError,
    ApprovalDecision,
    ApprovalState,
    Decision,
    Origin,
    Proposal,
    RiskTier,
    approval_state,
    derive_proposal_id,
    effective_tier,
    proposal_hash,
)
from .intent import canonical_json, derive_request_id, intent_hash, to_jsonable
from .kits import KIT_DIRS, KITS
from .money import CurrencyMismatch, Money, minor_units
from .ops import AirwallexPort, Operation, OperationResult, OperationStatus, ReadRequest
from .tenancy import (
    PLATFORM_BODY_FIELDS,
    TenantContext,
    TenantResolver,
    TenantRow,
    bind_operation,
    bind_platform_operation,
    bind_read,
    check_tenant_scope,
)
from .tools import SERVER_OWNED_FIELDS, ToolKind, ToolSpec

__all__ = [
    "KITS",
    "KIT_DIRS",
    "PLATFORM_BODY_FIELDS",
    "SERVER_OWNED_FIELDS",
    "AirwallexPort",
    "ApprovalBindingError",
    "ApprovalDecision",
    "ApprovalState",
    "CurrencyMismatch",
    "Decision",
    "Money",
    "Operation",
    "OperationResult",
    "OperationStatus",
    "Origin",
    "Proposal",
    "ReadRequest",
    "RiskTier",
    "TenantContext",
    "TenantResolver",
    "TenantRow",
    "ToolKind",
    "ToolSpec",
    "approval_state",
    "bind_operation",
    "bind_platform_operation",
    "bind_read",
    "canonical_json",
    "check_tenant_scope",
    "derive_proposal_id",
    "derive_request_id",
    "effective_tier",
    "intent_hash",
    "minor_units",
    "proposal_hash",
    "to_jsonable",
]
