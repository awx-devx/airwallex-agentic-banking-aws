"""DynamoDB access for proposals, approval decisions, tenants and the audit trail.

Every document is stored as one awx_common.transport JSON string attribute (`doc`), so
amounts and timestamps round-trip exactly. Keys:

proposals  pk proposal_id      written once by the kit-step Lambda; task_token and outcome
                               added later; GSI by_tenant (tenant_id, created_at)
decisions  pk proposal_id, sk approver   written only by the approval Lambda, once per approver
tenants    pk tenant_id        connected_account_id; seeded by the deploy, read server-side
audit      pk proposal_id, sk at#seq     append-only
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from awx_common.approvals import ApprovalDecision, ApprovalState, Proposal
from awx_common.errors import TenantScopeViolation
from awx_common.tenancy import (
    CONNECTED_ACCOUNT_KITS,
    TenantContext,
    platform_tenant,
    tenant_from_row,
)
from awx_common.transport import dumps, loads
from awx_common.workflow import PolicyDecision

from . import codec, engine


class DuplicateDecision(Exception):
    """This approver has already decided on this proposal."""


BY_TENANT_INDEX = "by_tenant"
BY_APPROVER_INDEX = "by_approver"  # (approver_tenant, created_at)
OUTCOME_VALUES = frozenset({s.value for s in ApprovalState} | {o.value for o in engine.Outcome})


@dataclass(frozen=True)
class StoredProposal:
    proposal: Proposal
    decision: PolicyDecision
    tenant_id: str
    task_token: str | None
    parent_proposal_id: str | None
    outcome: str | None = None
    execution_arn: str | None = None
    lost_children: tuple[str, ...] = ()  # child executions this run lost
    approver_tenant_id: str | None = None  # None on a row with no approver_tenant attribute

    @property
    def approver_tenant(self) -> str | None:
        """The tenant whose approvers may decide. A row with no approver_tenant attribute
        decides in its own tenant for kits 1-4; for kits 5-8 it is None: nobody may decide it,
        because the row cannot say whether its money action belonged to the kit's operator
        (fail closed)."""
        if self.approver_tenant_id:
            return self.approver_tenant_id
        if self.proposal.kit in CONNECTED_ACCOUNT_KITS:
            return None
        return self.tenant_id


def _client(client: Any | None, service: str = "dynamodb") -> Any:
    if client is not None:
        return client
    import boto3

    return boto3.client(service)


class ProposalRepo:
    def __init__(self, table_name: str, *, client: Any | None = None) -> None:
        self._table = table_name
        self._db = _client(client)

    def put(self, run: engine.Run, *, approver_tenant: str) -> Proposal:
        """Create the proposal once and return the stored one. A redelivered start with the
        same id and hash returns the first proposal (its timestamps win); another hash, tenant
        or approver_tenant (from awx_common.workflow.approver_tenant) fails."""
        p = run.proposal
        if not approver_tenant:
            raise ValueError("approver_tenant is required")
        try:
            self._db.put_item(
                TableName=self._table,
                Item={
                    "proposal_id": {"S": p.proposal_id},
                    "kit": {"S": p.kit},
                    "action": {"S": p.action},
                    "tenant_id": {"S": run.tenant.tenant_id},
                    "approver_tenant": {"S": approver_tenant},
                    "created_at": {"S": p.created_at.isoformat()},
                    "intent_hash": {"S": p.intent_hash},
                    "expires_at": {"N": str(int(p.expires_at.timestamp()))},
                    "doc": {"S": dumps({"proposal": p, "decision": run.decision})},
                    **(
                        {"parent_proposal_id": {"S": run.parent_proposal_id}}
                        if run.parent_proposal_id
                        else {}
                    ),
                },
                ConditionExpression="attribute_not_exists(proposal_id)",
            )
        except self._db.exceptions.ConditionalCheckFailedException:
            existing = self.get(p.proposal_id)
            if (
                existing is None
                or existing.proposal.intent_hash != p.intent_hash
                or existing.tenant_id != run.tenant.tenant_id
                or existing.approver_tenant != approver_tenant
            ):
                raise ValueError(
                    f"proposal {p.proposal_id} already exists with another intent"
                ) from None
            return existing.proposal
        return p

    def set_task_token(
        self, proposal_id: str, token: str, execution_arn: str | None = None
    ) -> None:
        """Store the approval wait's task token and the execution waiting on it, each in its
        own attribute (never inside `outcome`)."""
        values: dict[str, Any] = {":t": {"S": token}}
        update = "SET task_token = :t"
        if execution_arn:
            update += ", execution_arn = :e"
            values[":e"] = {"S": execution_arn}
        self._db.update_item(
            TableName=self._table,
            Key={"proposal_id": {"S": proposal_id}},
            UpdateExpression=update,
            ConditionExpression="attribute_exists(proposal_id)",
            ExpressionAttributeValues=values,
        )

    def set_outcome(self, proposal_id: str, outcome: str | None) -> None:
        """Write the `outcome` attribute the agent plane's tool Lambda reads (with proposal_id
        only): an ApprovalState value or a run Outcome value, nothing else."""
        if outcome is None:
            return
        if outcome not in OUTCOME_VALUES:
            raise ValueError(f"not an outcome value: {outcome!r}")
        self._db.update_item(
            TableName=self._table,
            Key={"proposal_id": {"S": proposal_id}},
            UpdateExpression="SET outcome = :o",
            ConditionExpression="attribute_exists(proposal_id)",
            ExpressionAttributeValues={":o": {"S": outcome}},
        )

    def set_lost_children(self, proposal_id: str, child_ids: Sequence[str]) -> None:
        """The child proposal IDs whose executions this run lost (a string set, added to,
        never removed), read by run_status."""
        if not child_ids:
            return
        self._db.update_item(
            TableName=self._table,
            Key={"proposal_id": {"S": proposal_id}},
            UpdateExpression="ADD lost_children :l",
            ConditionExpression="attribute_exists(proposal_id)",
            ExpressionAttributeValues={":l": {"SS": sorted(set(child_ids))}},
        )

    def get(self, proposal_id: str) -> StoredProposal | None:
        item = self._db.get_item(
            TableName=self._table,
            Key={"proposal_id": {"S": proposal_id}},
            ConsistentRead=True,
        ).get("Item")
        return None if item is None else self._stored(item)

    def list_for_tenant(self, tenant_id: str, *, limit: int = 50) -> list[StoredProposal]:
        """Newest first. The index projects every attribute, so no second read is needed."""
        return self._query(BY_TENANT_INDEX, "tenant_id", tenant_id, limit)

    def list_for_approver(self, tenant_id: str, *, limit: int = 50) -> list[StoredProposal]:
        """Proposals this tenant's approvers decide (a row with no approver_tenant is found by
        list_for_tenant instead)."""
        return self._query(BY_APPROVER_INDEX, "approver_tenant", tenant_id, limit)

    def _query(self, index: str, key: str, value: str, limit: int) -> list[StoredProposal]:
        resp = self._db.query(
            TableName=self._table,
            IndexName=index,
            KeyConditionExpression=f"{key} = :t",
            ExpressionAttributeValues={":t": {"S": value}},
            ScanIndexForward=False,
            Limit=limit,
        )
        return [self._stored(i) for i in resp["Items"]]

    @staticmethod
    def _stored(item: Mapping[str, Any]) -> StoredProposal:
        doc = loads(item["doc"]["S"])
        return StoredProposal(
            proposal=codec.proposal_from(doc["proposal"]),
            decision=codec.policy_decision_from(doc["decision"]),
            tenant_id=item["tenant_id"]["S"],
            task_token=item.get("task_token", {}).get("S"),
            parent_proposal_id=item.get("parent_proposal_id", {}).get("S"),
            outcome=item.get("outcome", {}).get("S"),
            execution_arn=item.get("execution_arn", {}).get("S"),
            lost_children=tuple(item.get("lost_children", {}).get("SS", ())),
            approver_tenant_id=item.get("approver_tenant", {}).get("S"),
        )


class DecisionRepo:
    def __init__(self, table_name: str, *, client: Any | None = None) -> None:
        self._table = table_name
        self._db = _client(client)

    def put(self, decision: ApprovalDecision) -> None:
        try:
            self._db.put_item(
                TableName=self._table,
                Item={
                    "proposal_id": {"S": decision.proposal_id},
                    "approver": {"S": decision.approver},
                    "doc": {"S": dumps(decision)},
                },
                ConditionExpression="attribute_not_exists(approver)",
            )
        except self._db.exceptions.ConditionalCheckFailedException:
            raise DuplicateDecision(decision.approver) from None

    def list(self, proposal_id: str) -> Sequence[ApprovalDecision]:
        out: list[ApprovalDecision] = []
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": "proposal_id = :p",
            "ExpressionAttributeValues": {":p": {"S": proposal_id}},
            "ConsistentRead": True,
        }
        while True:
            resp = self._db.query(**kwargs)
            out.extend(codec.approval_decision_from(loads(i["doc"]["S"])) for i in resp["Items"])
            if "LastEvaluatedKey" not in resp:
                return out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


class DynamoDbTenantResolver:
    """Implements awx_common.tenancy.TenantResolver from the tenants table."""

    def __init__(self, table_name: str, *, client: Any | None = None) -> None:
        self._table = table_name
        self._db = _client(client)

    def resolve(self, tenant_id: str | None, *, kit: str) -> TenantContext:
        """A row belongs to one kit (`kit`), and one with no connected_account_id must
        have `role` "operator". Anything else, a row with no kit included, is refused."""
        tenant_id = engine.resolve_tenant_id(tenant_id)
        if tenant_id == engine.PLATFORM_TENANT_ID:
            return platform_tenant(kit)
        item = self._db.get_item(
            TableName=self._table, Key={"tenant_id": {"S": tenant_id}}, ConsistentRead=True
        ).get("Item")
        if item is None:
            raise TenantScopeViolation("unknown tenant")
        return tenant_from_row(
            tenant_id,
            kit=kit,
            row_kit=item.get("kit", {}).get("S"),
            # present-but-not-S (an N, a NULL...) is passed as is, and refused
            account=_account_attr(item),
            role=item.get("role", {}).get("S"),
        )


def _account_attr(item: Mapping[str, Any]) -> object:
    """None only when the row has no connected_account_id attribute at all."""
    raw = item.get("connected_account_id")
    if raw is None:
        return None
    if isinstance(raw, Mapping) and set(raw) == {"S"}:
        return raw["S"]
    return raw


class AuditRepo:
    """One record per run (sk "run"), written with attribute_not_exists, so a retried Audit
    state cannot overwrite or duplicate it."""

    SORT_KEY = "run"

    def __init__(self, table_name: str, *, client: Any | None = None) -> None:
        self._table = table_name
        self._db = _client(client)

    def append(self, entry: Mapping[str, Any]) -> bool:
        """Write the run's audit record. Returns False if it was already written."""
        try:
            self._db.put_item(
                TableName=self._table,
                Item={
                    "proposal_id": {"S": entry["proposal_id"]},
                    "at": {"S": self.SORT_KEY},
                    "state": {"S": entry["state"]},
                    "doc": {"S": dumps(entry)},
                },
                ConditionExpression="attribute_not_exists(proposal_id)",
            )
        except self._db.exceptions.ConditionalCheckFailedException:
            return False
        return True
