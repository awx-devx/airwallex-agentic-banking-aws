"""Mutation adapter Lambda: the only code path that sends an Airwallex write.

Input  {"mode": "send"|"resend"|"advance"|"complete", "operation": Operation,
        "proposal_id": str, "result": OperationResult (complete only)}
Output {"result": OperationResult}

Invokable only by KitWorkflow state machine roles. Before any network call or store write it
re-checks, from the tables rather than from its input:

- the proposal exists, is intact, and its stored decisions approve it (or it needed none);
  for a fan-out child, the parent proposal is approved too
- the operation carries the proposal's kit and intent hash
- tenancy.check_tenant_scope against the tenant recorded on the proposal (defence in
  depth: the runner already checked it before dispatching)

A refusal raises NotAuthorised or TenantScopeViolation (the state machine sends those to Error;
nothing was sent). Then adapter_core.dispatch runs the mode over a MutationExecutor with ONE
attempt per call and the DynamoDB operation store, with an S3EvidenceStore scoped to the
operation's kit and the proposal's tenant for files.upload. The result, including
OUTCOME_UNKNOWN, is returned; every resend is the runner's decision (rule 9). The body goes out
exactly as the kit built it.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from typing import Any

from awx_common.approvals import ApprovalState, approval_state
from awx_common.ops import Operation
from awx_common.tenancy import check_tenant_scope
from awx_common.transport import decode, encode

from .. import adapter_core, codec
from ..credentials import SecretsManagerCredentials
from ..evidence import S3EvidenceStore
from ..repos import DecisionRepo, DynamoDbTenantResolver, ProposalRepo, StoredProposal
from ..store import DynamoDbOperationStore
from ._common import boto_client, env, log

_OK = (ApprovalState.APPROVED, ApprovalState.NOT_REQUIRED)


class NotAuthorised(Exception):
    """The operation is not covered by an approved proposal. Nothing was sent."""


@functools.cache
def _port() -> Any:
    from awx_spine.client import AirwallexClient

    return AirwallexClient(SecretsManagerCredentials(env("AWX_SECRET_ARN")))


def _executor(kit: str, tenant_id: str) -> Any:
    from awx_spine.executor import MutationExecutor

    store = DynamoDbOperationStore(env("AWX_OPERATIONS_TABLE"), client=boto_client("dynamodb"))
    evidence = S3EvidenceStore(
        env("AWX_ARTIFACTS_BUCKET"), kit=kit, tenant_id=tenant_id, client=boto_client("s3")
    )
    executor = MutationExecutor(
        _port(),
        store,
        max_attempts=adapter_core.MAX_ATTEMPTS_PER_CALL,
        evidence_store=evidence,
        clock=_now,  # explicit, the same wall clock as the store's
    )
    return executor, store


def _now() -> datetime:
    return datetime.now(UTC)


def _approved(stored: StoredProposal, now: datetime) -> ApprovalState:
    decisions = DecisionRepo(env("AWX_DECISIONS_TABLE"), client=boto_client("dynamodb"))
    return approval_state(stored.proposal, decisions.list(stored.proposal.proposal_id), now=now)


def authorise(op: Operation, proposal_id: str, *, now: datetime) -> StoredProposal:
    proposals = ProposalRepo(env("AWX_PROPOSALS_TABLE"), client=boto_client("dynamodb"))
    stored = proposals.get(proposal_id)
    if stored is None:
        raise NotAuthorised("no such proposal")
    state = _approved(stored, now)
    if state not in _OK:
        raise NotAuthorised(f"proposal is {state.value}")
    if stored.parent_proposal_id is not None:
        parent = proposals.get(stored.parent_proposal_id)
        if parent is None or _approved(parent, now) not in _OK:
            raise NotAuthorised("the parent proposal is not approved")
    p = stored.proposal
    if op.kit != p.kit or op.intent_hash != p.intent_hash:
        raise NotAuthorised("operation is not bound to this proposal")
    tenants = DynamoDbTenantResolver(env("AWX_TENANTS_TABLE"), client=boto_client("dynamodb"))
    check_tenant_scope(op, tenants.resolve(stored.tenant_id, kit=p.kit))  # this kit's row
    return stored


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    payload = decode(event)
    op = codec.operation_from(payload["operation"])
    mode = payload.get("mode") or "send"
    proposal_id = payload["proposal_id"]
    stored = authorise(op, proposal_id, now=datetime.now(UTC))
    given = codec.result_from(payload["result"]) if payload.get("result") else None
    executor, store = _executor(op.kit, stored.tenant_id)
    result = adapter_core.dispatch(executor, store, op, mode, given)
    log(
        "adapter.result",
        kit=op.kit,
        op=op.name,
        mode=mode,
        request_id=op.request_id,
        proposal_id=proposal_id,
        status=result.status.value,
        http_status=result.http_status,
        error_type=result.error_type,
        error_code=result.error_code,
    )
    return encode({"result": result})
