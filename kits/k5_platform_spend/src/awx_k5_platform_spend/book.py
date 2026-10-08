"""The platform book: kit 5 state that spans tenants, over `PolicyContext.platform_store`.

- `PlatformSetup` is static platform configuration loaded at deploy: the bridge-capital policy,
  the tenant map, the obligations the platform registered, fee schedules and repayment record.
  None of it is model input and none of it changes at run time.
- `PlatformBook(setup, store)` is built per call over the run's `ctx.platform_store` (the kit's
  KitStore for tenant "platform"). It holds the capital ledger (with the platform book id,
  M2) and today's bridge queue. Nothing is held in process memory between calls.

Each tenant's own registries (cards issued, simulations run, the tenant book id) live in that
tenant's `ctx.store` (see workflow.py).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType

from awx_common import Money, TenantContext
from awx_common.errors import NotFound, TenantScopeViolation
from awx_common.store import KitStore, VersionConflict

from .allocator import BridgeCandidate
from .config import PlatformConfig
from .ledger import CapitalLedger
from .tenants import TenantMap

QUEUE_PREFIX = "queue/"


@dataclass(frozen=True)
class Obligation:
    """A customer payment due, registered by the platform (not by the model)."""

    obligation_id: str
    tenant_id: str
    amount_due: Money
    due_on: date
    expected_deposit_on: date
    description: str = ""


def queue_key(tenant_id: str, obligation_id: str) -> str:
    return f"{QUEUE_PREFIX}{tenant_id}/{obligation_id}"


@dataclass(frozen=True)
class PlatformSetup:
    config: PlatformConfig
    tenants: TenantMap
    obligations: Mapping[str, Obligation] = field(default_factory=dict)
    monthly_fees: Mapping[str, Money] = field(default_factory=dict)  # tenant_id -> fee
    late_repayments: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        config: PlatformConfig,
        tenants: TenantMap,
        *,
        obligations: Iterable[Obligation] = (),
        monthly_fees: Mapping[str, Money] | None = None,
        late_repayments: Mapping[str, int] | None = None,
    ) -> PlatformSetup:
        by_id: dict[str, Obligation] = {}
        for o in obligations:
            tenants.resolve(o.tenant_id)  # must be a known tenant
            if o.amount_due.currency != config.currency or o.amount_due.amount <= 0:
                raise ValueError("obligation must be positive and in the platform currency")
            if o.obligation_id in by_id:
                raise ValueError("duplicate obligation id")
            by_id[o.obligation_id] = o
        return cls(
            config,
            tenants,
            MappingProxyType(by_id),
            MappingProxyType(dict(monthly_fees or {})),
            MappingProxyType(dict(late_repayments or {})),
        )


class PlatformBook:
    """Built per call; every read goes to the store."""

    def __init__(self, setup: PlatformSetup, store: KitStore) -> None:
        self.setup = setup
        self.store = store
        self.ledger = CapitalLedger(store, setup.config.reserve_floor)

    @property
    def config(self) -> PlatformConfig:
        return self.setup.config

    @property
    def tenants(self) -> TenantMap:
        return self.setup.tenants

    @property
    def obligations(self) -> Mapping[str, Obligation]:
        return self.setup.obligations

    @property
    def monthly_fees(self) -> Mapping[str, Money]:
        return self.setup.monthly_fees

    @property
    def late_repayments(self) -> Mapping[str, int]:
        return self.setup.late_repayments

    @property
    def book_id(self) -> str | None:
        return self.ledger.book_id

    def obligation(self, tenant: TenantContext, obligation_id: str) -> Obligation:
        """The tenant's own obligation. Another tenant's id is a scope violation, not a miss."""
        self.tenants.record(tenant)
        o = self.obligations.get(obligation_id)
        if o is None:
            raise NotFound("no such obligation")
        if o.tenant_id != tenant.tenant_id:
            raise TenantScopeViolation("obligation belongs to another tenant")
        return o

    # ---- bridge queue (written only by builds, postconditions and on_abandon) ------------

    def queue_entry(self, key: tuple[str, str]) -> tuple[BridgeCandidate, datetime] | None:
        item = self.store.get(queue_key(*key))
        if item is None or item.value.get("removed"):
            return None
        return _candidate(item.value), item.value["observed_at"]

    def register_candidate(self, c: BridgeCandidate, *, observed_at: datetime) -> None:
        """Write or refresh today's queue place. Idempotent; last observation wins."""
        key = queue_key(*c.key)
        value = {
            "tenant_id": c.tenant_id,
            "obligation_id": c.obligation_id,
            "shortfall": c.shortfall,
            "due_on": c.due_on.isoformat(),
            "expected_deposit_on": c.expected_deposit_on.isoformat(),
            "observed_at": observed_at,
            "removed": False,
        }
        for _ in range(8):
            item = self.store.get(key)
            try:
                self.store.put(key, value, expected_version=None if item is None else item.version)
                return
            except VersionConflict:
                continue
        raise RuntimeError("queue write kept conflicting")

    def drop_candidate(self, key: tuple[str, str]) -> None:
        """Remove a queue place (a tombstone, since the store has no delete). Idempotent."""
        skey = queue_key(*key)
        for _ in range(8):
            item = self.store.get(skey)
            if item is None or item.value.get("removed"):
                return
            try:
                self.store.put(skey, {**item.value, "removed": True}, expected_version=item.version)
                return
            except VersionConflict:
                continue
        raise RuntimeError("queue write kept conflicting")

    def ranking_pool(
        self, *, today: date, now: datetime, max_age: timedelta, exclude: Iterable[tuple[str, str]]
    ) -> list[BridgeCandidate]:
        """Live queue places for today. Pure: reads only.

        Skips tombstones, places observed more than `max_age` ago (their shortfall is stale),
        other days, and `exclude`. A tenant already holding or owing capital keeps its place but
        is marked `has_open_advance`, so the allocator treats it as ineligible and never counts
        its shortfall against headroom. Facts are refreshed from the ledger and platform config."""
        skip = set(exclude)
        out: list[BridgeCandidate] = []
        for item in self.store.query(QUEUE_PREFIX):
            if item.value.get("removed"):
                continue
            c = _candidate(item.value)
            if c.key in skip or c.due_on != today:
                continue
            if now - item.value["observed_at"] > max_age:
                continue
            out.append(
                BridgeCandidate(
                    tenant_id=c.tenant_id,
                    obligation_id=c.obligation_id,
                    shortfall=c.shortfall,
                    due_on=c.due_on,
                    expected_deposit_on=c.expected_deposit_on,
                    late_repayments=self.late_repayments.get(c.tenant_id, 0),
                    has_open_advance=self.ledger.has_open_advance(c.tenant_id),
                    tenant_active=self._active(c.tenant_id),
                )
            )
        return out

    def _active(self, tenant_id: str) -> bool:
        try:
            return self.tenants.record(self.tenants.resolve(tenant_id)).active
        except TenantScopeViolation:
            return False


def _candidate(value: Mapping[str, object]) -> BridgeCandidate:
    return BridgeCandidate(
        tenant_id=str(value["tenant_id"]),
        obligation_id=str(value["obligation_id"]),
        shortfall=value["shortfall"],  # type: ignore[arg-type]
        due_on=date.fromisoformat(str(value["due_on"])),
        expected_deposit_on=date.fromisoformat(str(value["expected_deposit_on"])),
    )


# ---- the setup document in the platform store (deployed runs) ---------------------------

SETUP_KEY = "setup"


def encode_setup(setup: PlatformSetup) -> dict[str, object]:
    cfg = setup.config
    return {
        "config": {
            "currency": cfg.currency,
            "reserve_floor": cfg.reserve_floor,
            "high_risk_bridge": cfg.high_risk_bridge,
            "max_bridge_per_tenant": cfg.max_bridge_per_tenant,
            "max_bridge_days": cfg.max_bridge_days,
            "card_programs": {
                n: {
                    "currency": p.currency,
                    "per_transaction_limit": p.per_transaction_limit,
                    "monthly_limit": p.monthly_limit,
                    "allowed_currencies": list(p.allowed_currencies),
                    "allowed_merchant_categories": list(p.allowed_merchant_categories),
                }
                for n, p in cfg.card_programs.items()
            },
        },
        "tenants": [
            {
                "tenant_id": r.tenant_id,
                "connected_account_id": r.connected_account_id,
                "display_name": r.display_name,
                "active": r.active,
            }
            for r in setup.tenants.records()
        ],
        "obligations": [
            {
                "obligation_id": o.obligation_id,
                "tenant_id": o.tenant_id,
                "amount_due": o.amount_due,
                "due_on": o.due_on.isoformat(),
                "expected_deposit_on": o.expected_deposit_on.isoformat(),
                "description": o.description,
            }
            for o in setup.obligations.values()
        ],
        "monthly_fees": dict(setup.monthly_fees),
        "late_repayments": dict(setup.late_repayments),
    }


def decode_setup(data: Mapping[str, object]) -> PlatformSetup:
    from .config import CardProgram
    from .tenants import TenantRecord

    c = data["config"]  # type: ignore[index]
    programs = {
        n: CardProgram(
            n,
            p["currency"],
            p["per_transaction_limit"],
            p["monthly_limit"],
            tuple(p["allowed_currencies"]),
            tuple(p["allowed_merchant_categories"]),
        )
        for n, p in c["card_programs"].items()  # type: ignore[index, union-attr]
    }
    config = PlatformConfig(
        currency=c["currency"],  # type: ignore[index]
        reserve_floor=c["reserve_floor"],  # type: ignore[index]
        high_risk_bridge=c["high_risk_bridge"],  # type: ignore[index]
        max_bridge_per_tenant=c["max_bridge_per_tenant"],  # type: ignore[index]
        max_bridge_days=int(c["max_bridge_days"]),  # type: ignore[index]
        card_programs=programs,
    )
    tenants = TenantMap(TenantRecord(**r) for r in data["tenants"])  # type: ignore[union-attr]
    obligations = [
        Obligation(
            o["obligation_id"],
            o["tenant_id"],
            o["amount_due"],
            date.fromisoformat(o["due_on"]),
            date.fromisoformat(o["expected_deposit_on"]),
            o["description"],
        )
        for o in data["obligations"]  # type: ignore[union-attr]
    ]
    return PlatformSetup.create(
        config,
        tenants,
        obligations=obligations,
        monthly_fees=data["monthly_fees"],  # type: ignore[arg-type]
        late_repayments=data["late_repayments"],  # type: ignore[arg-type]
    )


def seed_platform_setup(store: KitStore, setup: PlatformSetup) -> None:
    """Operator step at deploy: write (or replace) the platform setup document. Never called
    from a tool or a workflow; configuration is not model input."""
    item = store.get(SETUP_KEY)
    store.put(
        SETUP_KEY, encode_setup(setup), expected_version=None if item is None else item.version
    )


def load_platform_setup(store: KitStore) -> PlatformSetup | None:
    item = store.get(SETUP_KEY)
    return None if item is None else decode_setup(item.value)
