"""Kit-specific fixtures for running kit 3 through the real runner against FakeAirwallex.

The runner itself (rules 0 to 11, rule-9 reconcile, sandbox advances, the operation store) is
awx_spine_aws.local_runner over awx_spine's MutationExecutor. What is here is kit 3's own
setup: a funded fake with a beneficiary, a shared operation store and kit store across runs (as
in AWS, where both are DynamoDB tables), one fake clock wired through runner, executor and
stores, and small helpers to stage transfers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from awx_common import (
    ApprovalDecision,
    Decision,
    Money,
    Operation,
    OperationStatus,
    Proposal,
    TenantContext,
    derive_request_id,
)
from awx_common.approvals import Origin
from awx_common.store import InMemoryKitStore
from awx_common.workflow import KitWorkflowSpec, PolicyContext
from awx_k3_incident import SPECS
from awx_k3_incident import airwallex as awx
from awx_k3_incident.workflow import STAGING_SPECS
from awx_spine.fake import EPOCH, FakeAirwallex
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import WorkflowOutcome, run_kit_workflow

USD = "USD"
PLATFORM = TenantContext("platform")  # kit 3 runs on the platform's own account
BANK_RETURN = "BENEFICIARY_BANK_RETURNED"  # -> failure.code 90802, the one allowlisted code


@dataclass
class Kit3:
    fake: FakeAirwallex
    ops: InMemoryOperationStore = field(default_factory=InMemoryOperationStore)
    store: InMemoryKitStore = field(default_factory=InMemoryKitStore)
    beneficiary_id: str = ""
    audit: list[Mapping[str, Any]] = field(default_factory=list)

    @classmethod
    def new(cls, balance: str = "10000.00", **fake_kw: Any) -> Kit3:
        kit = cls(FakeAirwallex(balances={USD: Money.of(balance, USD)}, **fake_kw))
        kit.beneficiary_id = kit.create_beneficiary()
        return kit

    # ---- clock wiring ----
    def now(self) -> datetime:
        return EPOCH + timedelta(seconds=self.fake.clock())

    def advance(self, seconds: float) -> None:
        self.fake.advance(seconds)

    # ---- the real runner ----
    def run(
        self,
        action: str,
        raw: Mapping[str, Any],
        *,
        approve: bool | None = True,
        origin: Origin = Origin.AGENT,
        spec: KitWorkflowSpec | None = None,
        sandbox: bool = True,
        between: Callable[[], None] | None = None,
    ) -> WorkflowOutcome:
        """approve=None: nobody decides, so the proposal EXPIRES. `between` runs after the
        proposal is made and before the approval, as if the approval sat for a while."""

        def decide(p: Proposal) -> Sequence[ApprovalDecision]:
            if between is not None:
                between()
            if approve is None:
                return []
            verdict = Decision.APPROVE if approve else Decision.REJECT
            when = min(self.now(), p.expires_at)
            return [ApprovalDecision(p.proposal_id, p.intent_hash, "approver-sub", verdict, when)]

        return run_kit_workflow(
            spec or SPECS[action],
            raw,
            proposed_by="requester-sub",
            tenant=PLATFORM,
            port=self.fake,
            store=self.ops,
            decide=decide,
            now=self.now,
            sleep=self.fake.advance,
            audit=self.audit.append,
            kit_store=self.store,
            origin=origin,
            sandbox=sandbox,
        )

    def context(self) -> PolicyContext:
        return PolicyContext(PLATFORM, self.now(), self.fake.read, store=self.store)

    def read(self, action: str, raw: Mapping[str, Any]) -> Mapping[str, Any]:
        from awx_k3_incident import READS

        return READS[action](raw, self.context())

    # ---- setup ----
    def create_beneficiary(self) -> str:
        rid = derive_request_id("k3", "poc-i3-beneficiary", "create")
        op = Operation(
            kit="k3",
            name="beneficiaries.create",
            method="POST",
            path="/api/v1/beneficiaries/create",
            body={
                "request_id": rid,
                "nickname": "poc-i3-supplier",
                "transfer_methods": ["LOCAL"],
                "beneficiary": {
                    "type": "BANK_ACCOUNT",
                    "entity_type": "COMPANY",
                    "company_name": "poc-i3-supplier",
                    "address": {"country_code": "US", "state": "US-NY"},
                    "bank_details": {
                        "bank_account_category": "Checking",
                        "account_routing_type1": "aba",
                    },
                },
            },
            request_id=rid,
            business_key="poc-i3-beneficiary",
            intent_hash="b" * 64,
        )
        result = self.fake.execute(op)
        assert result.status is OperationStatus.SUCCEEDED, result
        return str(result.provider_id)

    def send(
        self, key: str = "inv-1001", amount: str = "10.00", stage: bool = True, **kw: Any
    ) -> WorkflowOutcome:
        """The operator staging path (STAGING_SPECS), never an agent tool."""
        kw.setdefault("spec", STAGING_SPECS["send_supplier_transfer"])
        kw.setdefault("origin", Origin.OPERATOR)
        return self.run(
            "send_supplier_transfer",
            {
                "payment_key": key,
                "beneficiary_id": self.beneficiary_id,
                "amount": amount,
                "currency": USD,
                "transfer_method": "LOCAL",
                "stage_for_incident": stage,
            },
            **kw,
        )

    def staged_original(self, key: str = "inv-1001") -> str:
        out = self.send(key)
        assert out.state == "SUCCEEDED", (out.state, out.detail)
        return str(out.results["send"][0].provider_id)

    def fail(self, transfer_id: str, failure_type: str | None = BANK_RETURN) -> None:
        """Drive the fake directly (test setup, not the kit): SENT, then FAILED."""
        if self.transfer(transfer_id)["status"] in ("SCHEDULED", "PROCESSING"):
            self.fake.simulate("transfer_transition", transfer_id=transfer_id, next_status="SENT")
        kw = {} if failure_type is None else {"failure_type": failure_type}
        self.fake.simulate(
            "transfer_transition", transfer_id=transfer_id, next_status="FAILED", **kw
        )

    def failed_original(self, failure_type: str | None = BANK_RETURN) -> str:
        oid = self.staged_original()
        self.fail(oid, failure_type)
        return oid

    def replace(self, oid: str, ref: str = "INC-1", **kw: Any) -> WorkflowOutcome:
        return self.run(
            "replace_failed_transfer", {"original_transfer_id": oid, "incident_ref": ref}, **kw
        )

    # ---- inspection ----
    def transfer(self, transfer_id: str) -> dict[str, Any]:
        (t,) = [t for t in self.fake.objects("transfers") if t["id"] == transfer_id]
        return t

    def replacements_of(self, oid: str) -> list[dict[str, Any]]:
        rids = set(awx.all_replacement_request_ids(oid))
        return [t for t in self.fake.objects("transfers") if t["request_id"] in rids]

    def sends(self, name: str = "transfers.create") -> list[tuple[str, str, str | None]]:
        path = {"transfers.create": "/api/v1/transfers/create"}[name]
        return [r for r in self.fake.requests if r[0] == "POST" and r[1] == path]

    def balance(self) -> Decimal:
        return self.fake.balance(USD).amount
