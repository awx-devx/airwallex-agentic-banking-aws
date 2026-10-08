"""Kit 4 fixtures over the REAL spine: awx_spine_aws.local_runner.run_kit_workflow against
awx_spine.FakeAirwallex (online payments enabled), awx_spine's InMemoryOperationStore and
MutationExecutor, with the fake clock wired into `now` and `sleep` (one clock through runner,
executor and stores).

Only kit-specific pieces live here:

- World: one fake sandbox, operation store, kit store and evidence store per test. It stages
  disputes the way the live scenario does (payment intent, confirm with the test card,
  simulation/pa/payment_disputes/create) and indexes the fixture evidence for each dispute
  (evidence.ingest_evidence plus the bytes under evidence.store_id), as the deployed upload
  handler does.
- Recorder: wraps the fake to record the kit's own writes for assertions, and injects the
  faults the fake does not model: a definitive 400 (REFUSE), an adapter crash after the send
  (the runner's engine.crashed path, rule 9), and the loss of a whole execution after a send
  (nothing after it runs, not even the postcondition).
- World.disputes edits the fake's stored disputes in place, for provider states the
  simulation endpoints cannot reach (REVERSED, PENDING_DECISION, a changed amount or due_at).
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from awx_common import (
    ApprovalDecision,
    Decision,
    Money,
    Operation,
    OperationResult,
    OperationStatus,
    ReadRequest,
    TenantContext,
)
from awx_common.approvals import Origin, Proposal
from awx_common.errors import ValidationFailed
from awx_common.evidence import InMemoryEvidenceStore
from awx_common.store import InMemoryKitStore
from awx_common.workflow import KitWorkflowSpec, PolicyContext, PolicyDecision, StepContext
from awx_k4_dispute import SPECS
from awx_k4_dispute.cases import ensure_book
from awx_k4_dispute.clearing import CLEAR_BOUND
from awx_k4_dispute.config import DEFAULT_CONFIG
from awx_k4_dispute.evidence import (
    EvidenceKind,
    ingest_evidence,
    inspect_bytes,
    store_id,
)
from awx_k4_dispute.simulation import (
    DemoCase,
    confirm_payment_intent,
    create_payment_intent,
    stage_dispute,
)
from awx_spine.executor import MutationExecutor
from awx_spine.fake import FakeAirwallex, Fault
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import RunRegistry, WorkflowOutcome, run_kit_workflow

NOW = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"
TENANT = TenantContext(tenant_id="merchant-demo")
KIT_WRITES = frozenset({"disputes.accept", "disputes.challenge", "files.upload"})
ALL_FIXTURE_DOCS = (
    "strong_fraud/ev_signature.jpg",
    "strong_fraud/ev_order_history.pdf",
    "small_not_received/ev_delivery_scan.jpg",
    "credit_not_processed/ev_support_emails.pdf",
    "injection/ev_customer_letter.pdf",
)
SANDBOX_LOCK = "/tmp/awx-sandbox.lock"  # noqa: S108 - the lock path every live sandbox test shares


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Lost(BaseException):
    """The whole execution died after a send: nothing after it ran, not even the
    postcondition (a Step Functions execution aborted or timed out mid-run)."""


@dataclass
class Recorder:
    """The AirwallexPort and FileUploadPort the runner talks to: the fake, plus recording and
    the faults the fake does not model."""

    fake: FakeAirwallex
    calls: list[Any] = field(default_factory=list)
    refuse: dict[str, int] = field(default_factory=dict)  # op -> times: definitive 400
    crash: dict[str, int] = field(default_factory=dict)  # op -> times: send, then raise
    lose: dict[str, int] = field(default_factory=dict)  # op -> times: send, then Lost
    crash_on_call: dict[str, int] = field(default_factory=dict)  # op -> the nth call crashes
    _seen: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def _take(table: dict[str, int], name: str) -> bool:
        if table.get(name, 0) > 0:
            table[name] -= 1
            return True
        return False

    def execute(self, op: Operation) -> OperationResult:
        if op.name in KIT_WRITES:
            self.calls.append(op)
        if self._take(self.refuse, op.name):
            self.fake.inject(op.name, Fault.REFUSE)  # the fake's definitive 400
        result = self.fake.execute(op)
        self._seen[op.name] = self._seen.get(op.name, 0) + 1
        if self.crash_on_call.get(op.name) == self._seen[op.name]:
            raise RuntimeError(f"adapter crashed after sending {op.name}")
        if self._take(self.crash, op.name):
            raise RuntimeError(f"adapter crashed after sending {op.name}")
        if self._take(self.lose, op.name):
            raise Lost(op.name)
        return result

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        return self.fake.read(req)

    def upload(self, data: bytes, **kw: Any) -> Mapping[str, Any]:
        import hashlib

        self.calls.append(
            SimpleNamespace(name="files.upload", sha256=hashlib.sha256(data).hexdigest(), **kw)
        )
        if self._take(self.refuse, "files.upload"):
            raise ValidationFailed("upload refused by the test", code="invalid_argument")
        return self.fake.upload(data, **kw)


class World:
    """One sandbox (fake), operation store, kit store and evidence store, with the clock wired."""

    def __init__(self, *, auto_ingest: bool = True, label_prefix: str = "poc-k4") -> None:
        self.label_prefix = label_prefix
        self.fake = FakeAirwallex(online_payments_enabled=True)
        self.port = Recorder(self.fake)
        self.ops = InMemoryOperationStore()
        self.kit_store = InMemoryKitStore()
        self.evidence = InMemoryEvidenceStore()
        self.auto_ingest = auto_ingest
        self.book = ensure_book(self.kit_store)
        self.audits: list[Mapping[str, Any]] = []
        self.runs = RunRegistry()  # one registry, so run_status sees earlier runs
        self._setup = MutationExecutor(self.fake, InMemoryOperationStore(), sleep=self.sleep)

    # ---- clock ---------------------------------------------------------------------------------
    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.fake.clock())

    def sleep(self, seconds: float) -> None:
        self.fake.advance(seconds)

    def at(self, when: datetime) -> None:
        delta = (when - self.now()).total_seconds()
        if delta > 0:
            self.fake.advance(delta)

    def seed_refund(self, payment_intent_id: str, amount: str, currency: str = "AUD") -> None:
        """A refund the merchant issued outside the kit. PRIVATE fake edit: FakeAirwallex has
        GET /pa/refunds but no refund create."""
        rid = f"rfd_seed_{len(self.fake._state.kind('refunds')) + 1}"
        self.fake._state.kind("refunds")[rid] = {
            "id": rid,
            "payment_intent_id": payment_intent_id,
            "amount": Decimal(amount),
            "currency": currency,
            "status": "SUCCEEDED",
            "_owner": "__platform__",
            "_t": self.fake.clock(),
        }

    # ---- staging ---------------------------------------------------------------------------
    @property
    def disputes(self) -> dict[str, dict[str, Any]]:
        return self.fake._state.kind("disputes")  # test-only edits of provider state

    def add_dispute(
        self,
        name: str,
        *,
        amount: str = "100.00",
        reason: Mapping[str, str] | None = None,
        stage: str = "RFI",
        currency: str = "AUD",
        due_at: str | None = None,
        **extra: Any,
    ) -> str:
        """Stage a dispute the live way, then file it under `name` for readable tests."""
        code = (reason or {}).get("code", "10.4")
        create_stage = stage if stage in ("RFI", "CHARGEBACK") else "RFI"
        case = DemoCase(
            name.replace("_", "-"),
            Money.of(amount, currency),
            code,
            create_stage,
            prefix=self.label_prefix,
        )
        pi = self._setup.run(create_payment_intent(case, self.book))
        assert pi.status is OperationStatus.SUCCEEDED, pi
        confirmed = self._setup.run(confirm_payment_intent(case, self.book, pi.provider_id, 2030))
        assert confirmed.status is OperationStatus.SUCCEEDED, confirmed
        staged = self._setup.run(stage_dispute(case, self.book, pi.provider_id, self.now()))
        assert staged.status is OperationStatus.SUCCEEDED, staged
        obj = self.disputes.pop(staged.provider_id)
        obj.update(id=name, stage=stage, due_at=due_at or iso(NOW + timedelta(days=7)))
        obj["created_at"] = iso(NOW - timedelta(days=1))
        obj.update(extra)
        self.disputes[name] = obj
        if self.auto_ingest:
            for rel in ALL_FIXTURE_DOCS:
                self.ingest(name, rel)
        return name

    def ingest(self, dispute_id: str, rel: str):
        path = FIXTURES / rel
        eid = path.stem
        data = path.read_bytes()
        entry = ingest_evidence(
            self.kit_store,
            dispute_id,
            eid,
            path.name,
            data,
            EVIDENCE_KINDS[eid],
            DEFAULT_CONFIG,
            text=evidence_text(rel),
        )
        self.evidence.put(
            store_id(dispute_id, eid),
            data,
            file_name=entry.file.file_name,
            content_type=entry.file.content_type,
        )
        return entry

    def escalate(self, dispute_id: str) -> None:
        self.fake.simulate("dispute_escalate", dispute_id=dispute_id)

    def resolve(self, dispute_id: str, outcome: str) -> None:
        if outcome in ("WON", "LOST"):
            self.fake.simulate("dispute_resolve", dispute_id=dispute_id, outcome=outcome)
        else:
            self.disputes[dispute_id]["status"] = outcome

    # ---- faults ----------------------------------------------------------------------------
    def timeout(self, op_name: str, *, landed: bool = True, visible_after_s: float | None = None):
        """landed: the write happens and the client times out (TIMEOUT_AFTER_SUBMIT);
        otherwise a 500 before anything happened (SERVER_ERROR, not applied)."""
        if landed:
            self.fake.inject(op_name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=visible_after_s)
        else:
            self.fake.inject(op_name, Fault.SERVER_ERROR)

    # ---- assertions ------------------------------------------------------------------------
    def writes(self, name: str | None = None) -> list[Any]:
        return [c for c in self.port.calls if name is None or c.name == name]

    def case(self, dispute_id: str) -> Mapping[str, Any]:
        item = self.kit_store.get(f"case/{dispute_id}")
        return {} if item is None else item.value


EVIDENCE_KINDS = {
    "ev_signature": EvidenceKind.DELIVERY_PROOF,
    "ev_order_history": EvidenceKind.ORDER_HISTORY,
    "ev_delivery_scan": EvidenceKind.DELIVERY_PROOF,
    "ev_support_emails": EvidenceKind.CUSTOMER_COMMUNICATION,
    "ev_customer_letter": EvidenceKind.CUSTOMER_COMMUNICATION,
}


@dataclass
class Outcome:
    """WorkflowOutcome plus what tests read most: the proposal's tier, reasons and computed."""

    raw: WorkflowOutcome

    @property
    def state(self) -> str:
        return self.raw.state

    @property
    def proposal(self) -> Proposal:
        return self.raw.proposal

    @property
    def decision(self) -> SimpleNamespace:
        p = self.raw.proposal
        return SimpleNamespace(risk_tier=p.risk_tier, reasons=p.reasons, computed=p.computed)

    @property
    def results(self) -> Mapping[str, Sequence[OperationResult]]:
        return self.raw.results

    @property
    def postcondition(self):
        return self.raw.postcondition

    @property
    def detail(self) -> str:
        return self.raw.detail or ""


def approve(*approvers: str, at: datetime | None = None, decision=Decision.APPROVE):
    def decide(p: Proposal):
        return [
            ApprovalDecision(p.proposal_id, p.intent_hash, a, decision, at or p.created_at)
            for a in approvers
        ]

    return decide


def run(
    spec: KitWorkflowSpec,
    raw: Mapping[str, Any],
    *,
    port: World,
    decide: Callable[[Proposal], Sequence[ApprovalDecision]] = lambda p: (),
    now: datetime | None = None,
    decided_at: datetime | None = None,
    proposed_by: str = "requester-1",
    origin: Origin = Origin.AGENT,
) -> Outcome:
    """Propose at `now`, approve at `decided_at` (the fake clock moves there while the
    proposal waits), then execute through the real runner."""
    w = port
    if now is not None:
        w.at(now)

    def deciding(p: Proposal):
        if decided_at is not None:
            w.at(decided_at)
        return decide(p)

    outcome = run_kit_workflow(
        spec,
        raw,
        proposed_by=proposed_by,
        tenant=TENANT,
        port=w.port,
        store=w.ops,
        decide=deciding,
        now=w.now,
        sleep=w.sleep,
        audit=w.audits.append,
        kit_store=w.kit_store,
        evidence_store=w.evidence,
        origin=origin,
        runs=w.runs,
        actions=tuple(SPECS),
    )
    return Outcome(outcome)


def make_ctx(
    w: World,
    spec: KitWorkflowSpec,
    now: datetime | None = None,
    *,
    proposal_id: str | None = "pid-direct",
) -> PolicyContext:
    """A context like the runner's, for calling policy, a read tool or one build directly."""

    def guarded(req: ReadRequest) -> Mapping[str, Any]:
        if not spec.allows_read(req.path):
            raise AssertionError(f"kit read outside its allowlist: {req.path}")
        return w.fake.read(req)

    return PolicyContext(
        tenant=TENANT,
        now=now or w.now(),
        read=guarded,
        store=w.kit_store,
        proposal_id=proposal_id,
        run_status=lambda pid, action: w.runs.status("k4", action, pid, TENANT.tenant_id),
    )


def build_at(
    spec: KitWorkflowSpec,
    raw: Mapping[str, Any],
    w: World,
    at: datetime,
    step_index: int,
    *,
    proposal_id: str = "pid-test",
):
    intent = spec.validate(raw)
    ctx = make_ctx(w, spec, at)
    decision = spec.policy(intent, ctx)
    step = spec.steps[step_index]
    return step.build(StepContext(intent, decision, ctx, "a" * 64, {}, proposal_id))


def policy_of(spec: KitWorkflowSpec, raw: Mapping[str, Any], w: World) -> PolicyDecision:
    return spec.policy(spec.validate(raw), make_ctx(w, spec))


# evidence fixtures ------------------------------------------------------------------------------


def evidence_file(rel: str):
    path = FIXTURES / rel
    eid = path.stem
    return inspect_bytes(eid, path.name, path.read_bytes(), EVIDENCE_KINDS[eid], DEFAULT_CONFIG)


def evidence_text(rel: str) -> str:
    return (FIXTURES / rel).with_suffix(".txt").read_text(encoding="utf-8")


def fact(kind: str, evidence_id: str, quote: str, order_ref: str | None = None) -> dict:
    out = {"kind": kind, "evidence_id": evidence_id, "quote": quote}
    if order_ref:
        out["order_ref"] = order_ref
    return out


STRONG_FILES = ("strong_fraud/ev_order_history.pdf", "strong_fraud/ev_signature.jpg")
STRONG_FACTS = [
    fact("SIGNED_DELIVERY", "ev_signature", "Signed by: J. Citizen"),
    fact(
        "DELIVERED_TO_BILLING_ADDRESS",
        "ev_signature",
        "Delivered 2026-09-20 14:02 to the billing address",
    ),
    fact(
        "DEVICE_FINGERPRINT_MATCH_PRIOR",
        "ev_order_history",
        "Device fingerprint dfp_7a1c matches all three prior orders",
    ),
    fact(
        "IP_MATCH_PRIOR",
        "ev_order_history",
        "IP address 203.0.113.24 matches all three prior orders",
    ),
    *[
        fact("PRIOR_UNDISPUTED_ORDER", "ev_order_history", line, ref)
        for ref, line in (
            ("ORD-1001", "Order ORD-1001 2026-03-02 AUD 210.00 not disputed"),
            ("ORD-1002", "Order ORD-1002 2026-05-14 AUD 340.00 not disputed"),
            ("ORD-1003", "Order ORD-1003 2026-07-30 AUD 125.00 not disputed"),
        )
    ],
]


def money(amount: str, currency: str = "AUD") -> dict:
    return {"amount": amount, "currency": currency}


def challenge_input(
    dispute_id: str, amount: str = "900.00", files=STRONG_FILES, facts=None, **over
) -> dict:
    raw = {
        "dispute_id": dispute_id,
        "disputed_amount": money(amount),
        "reason": "CARDHOLDER_AUTHORISED",
        "narrative": "The cardholder signed for delivery and the device and IP match three "
        "earlier undisputed orders.",
        "evidence_ids": sorted(Path(f).stem for f in files),
        "facts": STRONG_FACTS if facts is None else facts,
    }
    raw.update(over)
    return raw


def accept_input(
    dispute_id: str, amount: str, reason: str = "INSUFFICIENT_EVIDENCE", rationale: str = "r"
) -> dict:
    return {
        "dispute_id": dispute_id,
        "disputed_amount": money(amount),
        "reason": reason,
        "rationale": rationale,
    }


@contextmanager
def sandbox_lock() -> Iterator[None]:
    """A live sandbox run holds this for its whole duration, so two kits never race on shared
    platform balances."""
    with open(SANDBOX_LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def past_clear_bound(w: World) -> None:
    """Move the fake clock past the clear bound (attempt lease + finality + visibility)."""
    w.sleep(CLEAR_BOUND.total_seconds() + 1)
