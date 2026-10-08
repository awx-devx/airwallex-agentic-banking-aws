"""Kit 2 fixtures for the real runner: awx_spine_aws.local_runner over awx_spine.FakeAirwallex.

Only kit-specific glue lives here. Approval, rule 1's re-run, the operation store, rule 9's
reconcile and resend, rule 8's on_abandon and the tier floor are the runner's own code. One
clock drives runner, executor and stores: sleep=fake.advance and now from fake.clock.

Two hooks reach moments the runner does not expose:

- `between`: called once, just before the policy's second evaluation (rule 1's re-run), which
  is the moment between approval and execution. It wraps the spec's policy, nothing else.
- `before_build`: called once before a named step's first build (after rule 1's re-run).
- `before_write`: called once before the first send of a named operation (HookPort).

`crash_after` makes HookPort raise after the send left, as an adapter Lambda that dies after
its call (rule 9, `engine.crashed`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from awx_common import ApprovalDecision, Decision, Money, Origin
from awx_common.ops import Operation, OperationResult, ReadRequest
from awx_common.store import InMemoryKitStore, KitStore
from awx_common.tenancy import TenantContext
from awx_k2_purchase import SPECS
from awx_spine.fake import FakeAirwallex
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import WorkflowOutcome, run_kit_workflow

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
PLATFORM = TenantContext("platform")
SHA = "b" * 64

IMPERATIVE = re.compile(
    r"re-?propose|without approval|\bnow\b|\bshould\b|\bmust\b|\bplease\b|\btry\b",
    re.IGNORECASE,
)


def strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from strings(v)


def assert_no_instruction(value: Any, where: str) -> None:
    for text in strings(value):
        assert not IMPERATIVE.search(text), f"{where} instructs: {text!r}"


@dataclass
class HookPort:
    """AirwallexPort over the fake, with a before-write hook and crash-after-send."""

    fake: FakeAirwallex
    before_write: Mapping[str, Callable[[], None]] = field(default_factory=dict)
    crash_after: Mapping[str, Sequence[int]] = field(default_factory=dict)  # 1-based sends
    _fired: set[str] = field(default_factory=set)
    _sends: dict[str, int] = field(default_factory=dict)

    def execute(self, op: Operation) -> OperationResult:
        hook = self.before_write.get(op.name)
        if hook is not None and op.name not in self._fired:
            self._fired.add(op.name)
            hook()
        result = self.fake.execute(op)
        self._sends[op.name] = n = self._sends.get(op.name, 0) + 1
        if n in self.crash_after.get(op.name, ()):
            raise RuntimeError(f"adapter died after sending {op.name}")
        return result

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        return self.fake.read(req)


def guide_terms(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "purchase_ref": "crm-seats",
        "vendor": "Example CRM",
        "purchase_category": "SAAS_SUBSCRIPTION",
        "currency": "USD",
        "monthly_price": "1500.00",
        "annual_discount_percent": "18",
        "cancellation_notice_days": 30,
        "start_date": "2026-10-12",
        "renewal_date": "2027-10-12",
        "terms_document_sha256": SHA,
        "forecast_flows": [
            *(
                {
                    "date": (date(2026, 10, 12) + timedelta(days=7 * w - 1)).isoformat(),
                    "direction": "OUT",
                    "amount": "3000.00",
                    "label": "payroll and rent",
                }
                for w in range(1, 14)
            ),
            {"date": "2026-12-07", "direction": "IN", "amount": "20000.00", "label": "receipt"},
        ],
    }
    raw.update(over)
    return raw


def is_stale(out: WorkflowOutcome) -> bool:
    """Rule 1's fail-closed re-run: the runner reports FAILED with this detail."""
    return out.state == "FAILED" and "changed since the proposal" in (out.detail or "")


class K2Env:
    def __init__(
        self,
        *,
        balances: Mapping[str, str] | None = None,
        kit_store: KitStore | None = None,
        fake: FakeAirwallex | None = None,
    ) -> None:
        bal = balances or {"USD": "60000.00"}
        self.fake = fake or FakeAirwallex(balances={c: Money.of(a, c) for c, a in bal.items()})
        self.ops = InMemoryOperationStore()
        self.kit_store = kit_store or InMemoryKitStore()
        self.audit: list[Mapping[str, Any]] = []

    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.fake.clock())

    def approve(self, *approvers: str) -> Callable[[Any], Sequence[ApprovalDecision]]:
        def decide(p):
            return [
                ApprovalDecision(p.proposal_id, p.intent_hash, a, Decision.APPROVE, self.now())
                for a in approvers
            ]

        return decide

    def reject(self, approver: str = "approver"):
        def decide(p):
            when = self.now()
            return [ApprovalDecision(p.proposal_id, p.intent_hash, approver, Decision.REJECT, when)]

        return decide

    def run(
        self,
        action: str,
        raw: Mapping[str, Any],
        *approvers: str,
        decide: Callable[[Any], Sequence[ApprovalDecision]] | None = None,
        proposer: str = "requester",
        origin: Origin = Origin.AGENT,
        between: Callable[[], None] | None = None,
        before_write: Mapping[str, Callable[[], None]] | None = None,
        before_build: Mapping[str, Callable[[], None]] | None = None,
        build_twice: bool = False,
        crash_after: Mapping[str, Sequence[int]] | None = None,
        ops: InMemoryOperationStore | None = None,
    ) -> WorkflowOutcome:
        spec = SPECS[action]
        if between is not None:
            calls = {"n": 0}
            inner = spec.policy

            def policy(intent, ctx):
                calls["n"] += 1
                if calls["n"] == 2:
                    between()
                return inner(intent, ctx)

            spec = replace(spec, policy=policy)
        if build_twice:  # rule 0: every build runs twice; the first result is thrown away
            twice = []
            for step in spec.steps:

                def build(ctx, _inner=step.build):
                    _inner(ctx)
                    return _inner(ctx)

                twice.append(replace(step, build=build))
            spec = replace(spec, steps=tuple(twice))
        if before_build:
            steps = []
            for step in spec.steps:
                hook = before_build.get(step.name)
                if hook is None:
                    steps.append(step)
                    continue

                def build(ctx, _inner=step.build, _hook=hook, _done=[]):  # noqa: B006
                    if not _done:
                        _done.append(True)
                        _hook()
                    return _inner(ctx)

                steps.append(replace(step, build=build))
            spec = replace(spec, steps=tuple(steps))
        port = HookPort(self.fake, dict(before_write or {}), dict(crash_after or {}))
        out = run_kit_workflow(
            spec,
            raw,
            proposed_by=proposer,
            tenant=PLATFORM,
            port=port,
            store=ops or self.ops,
            decide=decide or self.approve(*approvers),
            now=self.now,
            sleep=self.fake.advance,
            audit=self.audit.append,
            kit_store=self.kit_store,
            origin=origin,
        )
        assert_no_instruction(out.proposal.reasons, f"{action} policy reasons")
        if out.detail and "StaleApproval" in out.detail:
            assert_no_instruction(out.detail, f"{action} StaleApproval")
        return out

    # ---- convenience ----------------------------------------------------------------------

    def issue(self, ref: str = "crm-seats", **over: Any) -> tuple[WorkflowOutcome, str]:
        out = self.run("issue_card", guide_terms(purchase_ref=ref, **over), "approver")
        assert out.state == "SUCCEEDED", (out.state, out.detail, out.postcondition)
        return out, out.results["card"][0].provider_id

    def limit_test_card(self) -> str:
        return self.issue("limit-test-crm")[1]

    def card(self, card_id: str) -> dict[str, Any]:
        (card,) = [c for c in self.fake.objects("cards") if c["card_id"] == card_id]
        return card

    def status(self, card_id: str) -> str:
        return self.card(card_id)["card_status"]

    def posts(self, path_part: str) -> list[str]:
        return [p for m, p, _ in self.fake.requests if m == "POST" and path_part in p]

    def card_updates(self) -> list[str]:
        return [p for p in self.posts("/issuing/cards/") if p.endswith("/update")]

    def authorisations(self) -> list[dict[str, Any]]:
        return self.fake.objects("card_transactions")

    def ctx(self):
        from awx_common.workflow import PolicyContext

        return PolicyContext(PLATFORM, self.now(), self.fake.read, store=self.kit_store)
