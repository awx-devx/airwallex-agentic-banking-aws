"""Kit 8 on the real spine: awx_spine_aws.local_runner.run_kit_workflow against FakeAirwallex.

All runner behaviour is the spine's own: propose, approval, the rule-1 recheck, scope checks,
the MutationExecutor, reconcile, resend, finality and the postcondition. The local_runner is
the code the Lambdas run. One clock drives everything: sleep=fake.advance, and `now` comes
from the fake clock.

Only kit-specific fixtures live here:
- `Market`: three seeded sellers, each with a connected account on the fake and a KitStore.
  The KitStore is in-memory, or DynamoDbKitStore on moto with `market(dynamodb=True)`.
- `KitPort`: a thin wrapper over the fake for the states and faults the fake does not model.
  The sandbox cannot provoke them either (FAKE-FIDELITY rows 109 and 51-52):
  - a platform movement that ends SUSPENDED, PENDING or FAILED;
  - a request still in transit that lands later;
  - a refusal at create;
  - an adapter crash after the send;
  - the uncertain-send reply shapes (OUTCOME_UNKNOWN with `refused`, and `multiple_objects`).
  Every other fault (TIMEOUT_AFTER_SUBMIT with invisibility, LIST_LAG, SERVER_ERROR applied,
  RATE_LIMIT, INSUFFICIENT_FUNDS, dedupe expiry) is the fake's own, through `fake.inject`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from awx_common import (
    ApprovalDecision,
    Decision,
    Money,
    Operation,
    OperationResult,
    OperationStatus,
    Origin,
    ReadRequest,
    TenantContext,
)
from awx_common.store import InMemoryKitStore
from awx_common.workflow import PolicyContext
from awx_k8_settlement import SPECS
from awx_k8_settlement import store as st
from awx_k8_settlement.evidence import Order
from awx_spine.fake import EPOCH, PLATFORM, FakeAirwallex, Fault
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import WorkflowOutcome, run_kit_workflow

st = st  # re-exported for tests that use h.st
NOW = EPOCH

USD = "USD"
SELLERS = ("alice", "bob", "cara")
PROFILES = {"alice": (900, "0.02"), "bob": (200, "0.03"), "cara": (30, "0.04")}
CYCLE1 = {"alice": ("10000", "200"), "bob": ("8000", "240"), "cara": ("5000", "200")}
KINDS = ("connected_account_transfers", "charges")
SETTLE, RECALC, RECOVER, RECONCILE, RESOLVE = (
    SPECS["settle_seller_cycle"],
    SPECS["recalculate_seller_reserve"],
    SPECS["recover_shortfall"],
    SPECS["reconcile_seller_money"],
    SPECS["resolve_in_flight_money"],
)
DAY = 86400.0


def usd(x: str | int | Decimal) -> Money:
    return Money.of(x, USD)


class Crash(Exception):
    """The adapter Lambda died after its request left (rule 9)."""


class KitPort:
    """FakeAirwallex plus the kit-specific fixtures above. Modes per op name, in order:

    ok        the fake as is (default)
    timeout   fake TIMEOUT_AFTER_SUBMIT; "timeout:<s>" keeps the object invisible for s seconds
    lag:<s>   fake LIST_LAG for s seconds after a successful create
    error500  fake SERVER_ERROR applied=True (the write happens, the reply is a 500)
    ratelimit fake RATE_LIMIT (429 before acceptance)
    nsf       fake INSUFFICIENT_FUNDS
    reject    refused at create (ValidationFailed), nothing sent
    lost      reply OUTCOME_UNKNOWN, nothing ever arrives
    late      reply OUTCOME_UNKNOWN, the request arrives later at deliver()
    crash     the request goes through, then the adapter dies (Crash)
    suspend / pending / fail  created, then held SUSPENDED / PENDING / FAILED (money returned)
    refused   OUTCOME_UNKNOWN with body {"refused": {...}}, nothing sent
    multiple  OUTCOME_UNKNOWN with error_code multiple_objects, nothing sent
    """

    def __init__(self, fake: FakeAirwallex) -> None:
        self.fake = fake
        self.executed: list[Operation] = []
        self.reads: list[ReadRequest] = []  # every GET the kit made, for scan assertions
        self.deferred: dict[str, Operation] = {}
        self.duplicated: set[str] = set()  # request_ids whose list read shows two objects
        self._modes: dict[str, list[str]] = {}

    def script(self, op_name: str, *modes: str) -> None:
        self._modes.setdefault(op_name, []).extend(modes)

    def clear(self) -> None:
        self._modes.clear()

    def deliver(self, request_id: str | None = None) -> None:
        for rid in [request_id] if request_id else list(self.deferred):
            op = self.deferred.pop(rid, None)
            if op is not None:
                self.fake.execute(op)  # a late arrival; a refused duplicate if one landed
        self.fake.advance(2)  # arrivals take time; platform movements settle after 1 s

    def set_status(self, request_id: str, status: str) -> None:
        """Move a stored platform movement, as an Airwallex operator would. SUSPENDED, PENDING
        and FAILED (money returned) go through the fake's public set_movement_status.
        SETTLED and CANCELLED have no public control, so they stay a private edit."""
        if status in ("SUSPENDED", "PENDING", "FAILED"):
            self.fake.set_movement_status(request_id, status)
            return
        for kind in KINDS:
            for obj in self.fake._state.kind(kind).values():
                if obj["request_id"] == request_id:
                    obj["status"] = status
                    return
        raise AssertionError(f"no movement under {request_id}")

    def execute(self, op: Operation) -> OperationResult:
        self.executed.append(op)
        queue = self._modes.get(op.name) or []
        mode = queue.pop(0) if queue else "ok"
        name, _, arg = mode.partition(":")
        if name == "reject":
            return _failed(op, "ValidationFailed", "validation_failed")
        if name in ("lost", "late"):
            if name == "late":
                self.deferred[op.request_id] = op
            return OperationResult(
                op.request_id,
                OperationStatus.OUTCOME_UNKNOWN,
                error_type="OutcomeUnknown",
                error_message="no reply",
            )
        if name == "refused":
            return OperationResult(
                op.request_id,
                OperationStatus.OUTCOME_UNKNOWN,
                error_type="OutcomeUnknown",
                body={"refused": {"error_type": "InsufficientFunds"}},
                error_message="resend refused while the first send may have landed",
            )
        if name == "multiple":
            return OperationResult(
                op.request_id,
                OperationStatus.OUTCOME_UNKNOWN,
                error_type="OutcomeUnknown",
                error_code="multiple_objects",
                error_message="two objects hold this request_id",
            )
        if name == "timeout":
            lag = float(arg) if arg else None
            self.fake.inject(op.name, Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=lag)
        elif name == "lag":
            self.fake.inject(op.name, Fault.LIST_LAG, visible_after_s=float(arg))
        elif name == "error500":
            self.fake.inject(op.name, Fault.SERVER_ERROR, applied=True)
        elif name == "ratelimit":
            self.fake.inject(op.name, Fault.RATE_LIMIT)
        elif name == "nsf":
            self.fake.inject(op.name, Fault.INSUFFICIENT_FUNDS)
        result = self.fake.execute(op)
        if name in ("suspend", "pending", "fail") and any(
            self.fake.find(k, op.request_id) for k in KINDS
        ):
            self.set_status(
                op.request_id,
                {"suspend": "SUSPENDED", "pending": "PENDING", "fail": "FAILED"}[name],
            )
        if name == "crash":
            raise Crash("adapter Lambda timed out after the request left")
        return result

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        self.reads.append(req)
        body = self.fake.read(req)
        rid = req.query.get("request_id") if req.query else None
        if rid in self.duplicated and isinstance(body.get("items"), list) and body["items"]:
            return {"items": [*body["items"], {**body["items"][0], "id": "tr_dupe"}]}
        return body

    # what really moved, for oracles
    def objects(self, kind: str, *, account: str | None = None) -> list[dict[str, Any]]:
        party = "source" if kind == "charges" else "destination"
        return [o for o in self.fake.objects(kind) if account is None or o[party] == account]


def _failed(op: Operation, error_type: str, code: str) -> OperationResult:
    return OperationResult(
        op.request_id,
        OperationStatus.FAILED,
        400,
        error_type=error_type,
        error_code=code,
        error_message=code,
    )


class Market:
    """Three sellers on one fake, run through the real local runner."""

    def __init__(
        self,
        platform_cash: Money | None = None,
        *,
        store_factory=None,
        seed: bool = True,
        twin_of: Market | None = None,
    ) -> None:
        self.fake = FakeAirwallex(balances={USD: platform_cash or usd(0)})
        self.port = KitPort(self.fake)
        self.ops = InMemoryOperationStore()
        self.own_cash = usd(0)  # the platform's own money (it pays buyer refunds it recovers)
        self.offset_s = 0.0  # extra time on the kit's clock only (dedupe-window tests)
        self.accounts = {
            s: self.fake.add_connected_account(f"poc-i8-{s}", {USD: usd(0)}) for s in SELLERS
        }
        make = store_factory or (lambda seller: InMemoryKitStore())
        self.stores = {s: make(s) for s in SELLERS}
        if seed:
            seed_sellers(
                self.stores, twin_of=twin_of.stores if twin_of else None, accounts=self.accounts
            )

    # clock -----------------------------------------------------------------------------------
    def now(self):
        return EPOCH + timedelta(seconds=self.fake.clock() + self.offset_s)

    def advance(self, seconds: float) -> None:
        self.fake.advance(seconds)

    # money -----------------------------------------------------------------------------------
    @property
    def platform_cash(self) -> Money:
        return self.fake.balance(USD)

    def fund(self, delta: Money) -> None:
        """Buyer money in (or refunds paid out) on the platform wallet, outside the kit, through
        the fake's public credit. The oracle can model refunds paid out of an empty
        wallet; the fake never goes below zero, so that one case stays a private edit."""
        try:
            self.fake.credit(USD, delta)
        except ValueError:
            self.fake._state.accounts[PLATFORM].bal(USD)[0] += delta.amount

    def set_wallet(self, seller: str, amount: Money) -> None:
        account = self.accounts[seller]
        self.fake.credit(USD, amount - self.fake.balance(USD, account), account=account)

    def wallet(self, seller: str) -> Money:
        return self.fake.balance(USD, self.accounts[seller])

    # running ---------------------------------------------------------------------------------
    def tenant(self, seller: str) -> TenantContext:
        return TenantContext(seller, self.accounts[seller])

    def run(
        self,
        spec,
        raw: Mapping[str, Any],
        seller: str,
        *,
        approve: bool = True,
        origin: Origin = Origin.AGENT,
        tamper: Callable[[], None] | None = None,
    ) -> WorkflowOutcome:
        def decide(proposal):
            if tamper is not None:
                tamper()  # between approval and the rule-1 recheck
            if not approve:
                return []
            return [
                ApprovalDecision(
                    proposal.proposal_id, proposal.intent_hash, who, Decision.APPROVE, self.now()
                )
                for who in ("approver-1", "approver-2")
            ]

        return run_kit_workflow(
            spec,
            raw,
            proposed_by="requester",
            tenant=self.tenant(seller),
            port=self.port,
            store=self.ops,
            decide=decide,
            now=self.now,
            sleep=self.fake.advance,
            kit_store=self.stores[seller],
            origin=origin,
        )

    def context(self, seller: str, spec=SETTLE) -> PolicyContext:
        """A read-tool context, as the reader Lambda gives one."""
        t = self.tenant(seller)

        def read(req: ReadRequest) -> Mapping[str, Any]:
            assert req.on_behalf_of in (None, t.connected_account_id)
            return self.port.read(req)

        return PolicyContext(
            tenant=t,
            now=self.now(),
            read=read,
            store=self.stores[seller],
            read_platform=self.port.read,
        )

    # ledger views ----------------------------------------------------------------------------
    def ledger(self, seller: str):
        return st.ledger_state(self.stores[seller], seller, USD).ledger

    def settled(self, seller: str, cycle_id: str):
        return st.settlement(st.ledger_state(self.stores[seller], seller, USD), cycle_id)

    def add_cycle(self, seller: str, cycle_id: str, entitlement, refunds, volume) -> None:
        """A cycle's figures, and the buyer money behind them on the platform wallet."""
        e, f = usd(entitlement), usd(refunds)
        st.put_cycle(self.stores[seller], st.CycleData(cycle_id, e, f, usd(volume)))
        self.fund(e - f)

    def transfers_to(self, seller: str) -> list[dict[str, Any]]:
        return self.port.objects("connected_account_transfers", account=self.accounts[seller])

    def charges_from(self, seller: str) -> list[dict[str, Any]]:
        return self.port.objects("charges", account=self.accounts[seller])

    def balanced(self) -> bool:
        from awx_k8_settlement import platform_residual

        ledgers = [self.ledger(s) for s in SELLERS]
        residual = platform_residual(ledgers, self.platform_cash, platform_own_cash=self.own_cash)
        return residual.is_zero()


def seed_sellers(stores, *, twin_of=None, accounts=None) -> None:
    """The guide's scenario: three sellers, the newest gets the highest reserve."""
    for s, (days, rate) in PROFILES.items():
        ent, ref = CYCLE1[s]
        st.seed_seller(
            stores[s],
            st.SellerProfile(s, USD, days, Decimal(rate)),
            cycles=(st.CycleData("c1", usd(ent), usd(ref), usd(ent)),),
            account=None if accounts is None else accounts[s],
        )
        if twin_of is not None:  # a counterfactual twin shares the book id
            item = stores[s].get("book")
            stores[s].put("book", twin_of[s].get("book").value, expected_version=item.version)
    bob = tuple(Order(f"bob-{i:03d}", "bob", usd(75), usd(0), False) for i in range(40))
    bob += tuple(Order(f"bob-d{i:02d}", "bob", usd(75), usd(0), True) for i in range(5))
    st.put_orders(stores["bob"], bob)
    st.put_orders(stores["alice"], (Order("alice-001", "alice", usd(999), usd(0), False),))
    event = {
        "event_id": "evt-bob-1",
        "seller": "bob",
        "carrier": "fastship",
        "event_type": "DELIVERY_FAILURE",
        "order_ids": [f"bob-{i:03d}" for i in range(40)] + ["bob-d00", "alice-001", "ghost-9"],
        "observed_at": "2026-10-06T01:00:00Z",
        "narrative": "Depot fire. 40 parcels undelivered. Refund requests have started.",
    }
    stores["bob"].put("evidence/evt-bob-1", event, expected_version=None)
    # A forged copy filed under alice: it names bob, so alice's tenant must refuse it.
    stores["alice"].put("evidence/evt-bob-1", event, expected_version=None)


def c1_net() -> Money:
    total = usd(0)
    for s in SELLERS:
        e, f = CYCLE1[s]
        total = total + usd(e) - usd(f)
    return total


def bob_c1_net() -> Money:
    return usd(8000 - 240)


def dump(kstore) -> str:
    """Every item a seller's store holds, for byte-identical comparisons (book version left
    out: a twin copies its book id by an extra write). The seeded `account` record is left out:
    a twin runs on its own fake, so its connected account ids differ."""
    from awx_common.transport import dumps

    return dumps(
        {
            i.key: {"v": i.value, "n": None if i.key == "book" else i.version}
            for i in kstore.query("")
            if i.key != "account"
        }
    )


class HookStore:
    """A KitStore wrapper that runs `callback` once, just before the next write of `key`, to
    interleave two runs exactly between one run's ledger read and its versioned write."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self._hook = None

    def arm(self, callback, key: str = "ledger") -> None:
        self._hook = (key, callback)

    def get(self, key):
        return self.inner.get(key)

    def query(self, prefix):
        return self.inner.query(prefix)

    def put(self, key, value, *, expected_version):
        if self._hook is not None and self._hook[0] == key:
            _, callback = self._hook
            self._hook = None
            callback()
        return self.inner.put(key, value, expected_version=expected_version)


# DynamoDB (moto) -------------------------------------------------------------------------------


@pytest.fixture
def dynamodb(monkeypatch):
    """A moto ledger table, shaped as the spine stack creates it."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb")
        client.create_table(
            TableName="ledger",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def dynamo_factory(client):
    from awx_spine_aws.store import DynamoDbKitStore

    return lambda seller: DynamoDbKitStore("ledger", kit="k8", tenant_id=seller, client=client)


@pytest.fixture
def market() -> Market:
    return Market(c1_net())


@pytest.fixture
def stores(market):
    return market.stores


@pytest.fixture
def h():
    """This module, so tests can use the harness under --import-mode=importlib."""
    return sys.modules[__name__]


# Operator commands: seeding a store exactly as a deploy would ---------------------------------


def platform_store(m: Market):
    """The kit's platform partition for operator commands: one per market, so the account index
    persists across commands."""
    if getattr(m, "op_platform", None) is None:
        m.op_platform = InMemoryKitStore()
    return m.op_platform


def op_context(m: Market, store_for, *, read=True):
    from awx_common.operator_commands import OperatorContext

    return OperatorContext(
        kit="k8",
        platform_store=platform_store(m),
        store_for=store_for,
        now=m.now(),
        # a platform-scope GET, as the deploy runner gives; a test may pass its own reader
        read=m.fake.read if read is True else (read or None),
    )


def op(m: Market, store_for, name: str, args, *, read=True):
    from awx_common.operator_commands import run_operator_command
    from awx_k8_settlement import OPERATOR_COMMANDS

    return run_operator_command(OPERATOR_COMMANDS, name, op_context(m, store_for, read=read), args)


def money_arg(x) -> dict[str, str]:
    return {"amount": str(usd(x).amount), "currency": USD}


BOB_EVENT = {
    "event_id": "evt-bob-1",
    "seller": "bob",
    "carrier": "fastship",
    "event_type": "DELIVERY_FAILURE",
    "order_ids": [f"bob-{i:03d}" for i in range(40)] + ["bob-d00", "alice-001", "ghost-9"],
    "observed_at": "2026-10-06T01:00:00Z",
    "narrative": "Depot fire. 40 parcels undelivered. Refund requests have started.",
}


def seed_through_commands(m: Market, store_for, sellers=SELLERS) -> None:
    """The guide's sellers, written only through OPERATOR_COMMANDS."""
    for s in sellers:
        days, rate = PROFILES[s]
        op(
            m,
            store_for,
            "seed_seller",
            {
                "seller": s,
                "connected_account_id": m.accounts[s],
                "currency": USD,
                "tenure_days": days,
                "trailing_refund_rate": rate,
            },
        )
        ent, ref = CYCLE1[s]
        op(
            m,
            store_for,
            "load_cycle",
            {
                "seller": s,
                "cycle_id": "c1",
                "entitlement": money_arg(ent),
                "refunds": money_arg(ref),
                "volume_base": money_arg(ent),
            },
        )
    if sellers != SELLERS:
        return
    rows = [
        {
            "order_id": f"bob-{i:03d}",
            "amount": money_arg(75),
            "refunded": money_arg(0),
            "delivered": False,
        }
        for i in range(40)
    ]
    rows += [
        {
            "order_id": f"bob-d{i:02d}",
            "amount": money_arg(75),
            "refunded": money_arg(0),
            "delivered": True,
        }
        for i in range(5)
    ]
    op(
        m,
        store_for,
        "load_orders",
        {"seller": "bob", "as_of": "2026-10-06T00:00:00Z", "orders": rows},
    )
    op(
        m,
        store_for,
        "load_orders",
        {
            "seller": "alice",
            "as_of": "2026-10-06T00:00:00Z",
            "orders": [
                {
                    "order_id": "alice-001",
                    "amount": money_arg(999),
                    "refunded": money_arg(0),
                    "delivered": False,
                }
            ],
        },
    )
    op(m, store_for, "load_carrier_events", {"seller": "bob", "events": [BOB_EVENT]})


def load_cycle(m: Market, store_for, seller, cycle_id, entitlement, refunds, volume) -> None:
    """add_cycle's deployed twin: the figures through load_cycle, then the buyer money behind
    them on the platform wallet. The command moves no money, so in a live run the
    deposit is the separate documented step."""
    op(
        m,
        store_for,
        "load_cycle",
        {
            "seller": seller,
            "cycle_id": cycle_id,
            "entitlement": money_arg(entitlement),
            "refunds": money_arg(refunds),
            "volume_base": money_arg(volume),
        },
    )
    m.fund(usd(entitlement) - usd(refunds))
