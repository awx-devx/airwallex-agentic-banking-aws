"""Allocator and ledger invariants: unit tests plus hypothesis property tests."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest
from awx_common import Money
from awx_common.store import InMemoryKitStore
from awx_k5_platform_spend import (
    BridgeCandidate,
    CapitalLedger,
    LedgerError,
    ReserveBreach,
    allocate,
    eligibility,
)
from awx_k5_platform_spend.allocator import DENY_FLOOR
from awx_k5_platform_spend.ledger import StaleObservation, WalletObservation
from hypothesis import given, settings
from hypothesis import strategies as st
from k5_support import NOW, TODAY, make_book, usd

CONFIG = make_book().config

cents = st.integers(min_value=-5_000, max_value=120_000).map(lambda c: Money.from_minor(c, "USD"))


@st.composite
def candidates(draw):
    n = draw(st.integers(0, 8))
    out = []
    for i in range(n):
        out.append(
            BridgeCandidate(
                tenant_id=f"t-{draw(st.integers(0, 4))}",
                obligation_id=f"ob-{i}",
                shortfall=draw(cents),
                due_on=TODAY + timedelta(days=draw(st.sampled_from([0, 0, 0, -1, 1]))),
                expected_deposit_on=TODAY + timedelta(days=draw(st.integers(-1, 9))),
                late_repayments=draw(st.integers(0, 2)),
                has_open_advance=draw(st.booleans()),
                tenant_active=draw(st.booleans()) or draw(st.booleans()),
            )
        )
    return out


@given(candidates(), cents)
def test_allocator_never_commits_more_than_headroom(pool, headroom):
    a = allocate(pool, headroom, CONFIG, TODAY)
    total = sum((c.shortfall for c in a.funded), Money.zero("USD"))
    assert (total <= headroom) or not a.funded
    if a.funded:
        assert a.remaining == headroom - total
        assert not a.remaining.is_negative()


@given(candidates(), cents)
def test_allocator_funds_only_eligible_whole_shortfalls_and_accounts_for_all(pool, headroom):
    a = allocate(pool, headroom, CONFIG, TODAY)
    assert {c.key for c in a.funded} | {c.key for c, _ in a.denied} == {c.key for c in pool}
    assert not ({c.key for c in a.funded} & {c.key for c, _ in a.denied})
    for c in a.funded:
        assert eligibility(c, CONFIG, TODAY) is None
        assert c in pool  # funded for its whole, unmodified shortfall


@given(candidates(), cents)
def test_allocator_is_maximal_greedy(pool, headroom):
    """Every eligible candidate denied on capital is larger than what was left over."""
    a = allocate(pool, headroom, CONFIG, TODAY)
    for c, reason in a.denied:
        if reason == DENY_FLOOR:
            assert eligibility(c, CONFIG, TODAY) is None
            assert c.shortfall > a.remaining


@given(candidates(), cents, st.randoms(use_true_random=False))
def test_allocator_ignores_input_order(pool, headroom, rnd):
    shuffled = list(pool)
    rnd.shuffle(shuffled)
    assert allocate(pool, headroom, CONFIG, TODAY) == allocate(shuffled, headroom, CONFIG, TODAY)


def _c(tenant, oid, short, **kw):
    return BridgeCandidate(tenant, oid, usd(short), TODAY, TODAY + timedelta(days=2), **kw)


def test_priority_prefers_clean_record_then_smaller_shortfall():
    pool = [_c("t-a", "1", "300"), _c("t-b", "2", "250"), _c("t-c", "3", "100", late_repayments=1)]
    a = allocate(pool, usd("400"), CONFIG, TODAY)
    assert [c.tenant_id for c in a.funded] == ["t-b", "t-c"]
    assert a.decision_for(("t-a", "1")) == (False, DENY_FLOOR, None)


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"tenant_active": False}, "tenant is not active"),
        ({"has_open_advance": True}, "tenant already has an open advance"),
        ({"due_on": TODAY + timedelta(days=1)}, "payment is not due today"),
        (
            {"expected_deposit_on": TODAY - timedelta(days=1)},
            "deposit is expected before the payment is due",
        ),
        (
            {"expected_deposit_on": TODAY + timedelta(days=8)},
            "deposit is expected too late to bridge",
        ),
        ({"shortfall": usd("800.01")}, "shortfall exceeds the per-tenant bridge limit"),
        ({"shortfall": usd("0")}, "no shortfall"),
        ({"shortfall": Money.of("10", "EUR")}, "shortfall is not in the platform currency"),
    ],
)
def test_eligibility_reasons(kw, reason):
    base = {
        "tenant_id": "t",
        "obligation_id": "o",
        "shortfall": usd("10"),
        "due_on": TODAY,
        "expected_deposit_on": TODAY + timedelta(days=1),
    }
    base.update(kw)
    assert eligibility(BridgeCandidate(**base), CONFIG, TODAY) == reason


def test_allocator_rejects_duplicates():
    with pytest.raises(ValueError, match="duplicate"):
        allocate([_c("t", "1", "1"), _c("t", "1", "2")], usd("10"), CONFIG, TODAY)


# ---- ledger ------------------------------------------------------------------------------


def test_ledger_flow_and_floor():
    led = CapitalLedger(InMemoryKitStore(), usd("600"))
    led.fund("cap", usd("1000"), at=NOW)
    assert led.headroom() == usd("400")
    led.place_hold("r1", "t-a", usd("300"), owner="o")
    hold, created = led.place_hold("r1", "t-a", usd("300"), owner="o2")
    assert hold.amount == usd("300")  # idempotent
    assert not created
    assert hold.owner == "o"
    with pytest.raises(LedgerError):
        led.place_hold("r1", "t-a", usd("301"), owner="o")
    with pytest.raises(ReserveBreach):
        led.place_hold("r2", "t-b", usd("100.01"), owner="o")
    led.post_advance("r1", "t-a", usd("300"), provider_id="cat_1", at=NOW)
    led.post_advance("r1", "t-a", usd("300"), provider_id="cat_1", at=NOW)  # duplicate delivery
    assert led.cash == usd("700")
    assert led.held == usd("0")
    with pytest.raises(LedgerError, match="exceeds"):
        led.post_repayment("p1", "t-a", usd("300.01"), provider_id="chg", at=NOW)
    led.post_repayment("p1", "t-a", usd("300"), provider_id="chg", at=NOW)
    led.post_fee("f1", "t-a", usd("25"), provider_id="chg2", at=NOW)
    assert led.cash == usd("1025")
    assert led.receivables() == {"t-a": usd("0")}
    with pytest.raises(LedgerError, match="different content"):
        led.post_fee("f1", "t-a", usd("26"), provider_id="chg2", at=NOW)
    with pytest.raises(LedgerError, match="must match its hold"):
        led.post_advance("r9", "t-a", usd("1"), provider_id="x", at=NOW)
    with pytest.raises(LedgerError, match="ledger is USD"):
        led.post_fee("f2", "t-a", Money.of("1", "EUR"), provider_id="x", at=NOW)


def test_hold_release_respects_its_owner_and_failures_move_the_attempt():
    led = CapitalLedger(InMemoryKitStore(), usd("0"))
    led.fund("cap", usd("100"), at=NOW)
    led.place_hold("r1", "t", usd("60"), owner="run-a")
    assert not led.release_hold("r1", owner="run-b")
    assert led.hold("r1") is not None
    assert led.record_failure("t:bridge:o", "r1") == 1
    assert led.record_failure("t:bridge:o", "r1") == 1  # idempotent per request_id
    assert led.hold("r1") is None
    assert led.failures("t:bridge:o") == 1


def test_wallet_cap_binds_even_when_the_ledger_has_room():
    led = CapitalLedger(InMemoryKitStore(), usd("0"))
    led.fund("cap", usd("100"), at=NOW)
    with pytest.raises(ReserveBreach):
        led.place_hold(
            "r1", "t", usd("60"), owner="o", wallet=WalletObservation(led.version, usd("59.99"))
        )
    with pytest.raises(StaleObservation):  # the ledger moved since the wallet was read
        led.place_hold(
            "r1", "t", usd("10"), owner="o", wallet=WalletObservation(led.version - 1, usd("100"))
        )
    _hold, created = led.place_hold(
        "r1", "t", usd("60"), owner="o", wallet=WalletObservation(led.version, usd("60"))
    )
    assert created


class _RacingStore(InMemoryKitStore):
    """Lets a second writer land between this writer's read and its conditional write."""

    def __init__(self):
        super().__init__()
        self.interleave = None

    def put(self, key, value, *, expected_version):
        if self.interleave is not None:
            fn, self.interleave = self.interleave, None
            fn()
        return super().put(key, value, expected_version=expected_version)


def test_concurrent_holds_cannot_both_take_the_last_headroom():
    store = _RacingStore()
    a = CapitalLedger(store, usd("600"))
    b = CapitalLedger(store, usd("600"))
    a.fund("cap", usd("1000"), at=NOW)
    store.interleave = lambda: b.place_hold("rb", "t-b", usd("300"), owner="o")
    with pytest.raises(ReserveBreach):
        a.place_hold("ra", "t-a", usd("300"), owner="o")  # re-read after the conflict: no room
    assert [h.key for h in a.holds()] == ["rb"]


ACTIONS = st.lists(
    st.tuples(
        st.sampled_from(["fund", "hold", "release", "settle", "repay", "fee"]),
        st.integers(0, 5),
        st.integers(1, 60_000),
    ),
    max_size=40,
)


@settings(max_examples=200)
@given(st.integers(0, 50_000), st.integers(0, 100_000), ACTIONS)
def test_ledger_invariants_hold_under_any_sequence(floor_c, capital_c, actions):
    floor = Money.from_minor(floor_c, "USD")
    led = CapitalLedger(InMemoryKitStore(), floor)
    if capital_c:
        led.fund("cap", Money.from_minor(capital_c, "USD"), at=NOW)
    rng = random.Random(capital_c)
    committed = False
    for i, (kind, tenant_n, amount_c) in enumerate(actions):
        tenant, amount = f"t-{tenant_n}", Money.from_minor(amount_c, "USD")
        try:
            if kind == "fund":
                led.fund(f"cap-{i}", amount, at=NOW)
            elif kind == "hold":
                led.place_hold(f"b-{i}", tenant, amount, owner="o")
                committed = True
            elif kind == "release" and led.holds():
                led.release_hold(rng.choice(led.holds()).key, owner="o")
            elif kind == "settle" and led.holds():
                h = rng.choice(led.holds())
                led.post_advance(h.key, h.tenant_id, h.amount, provider_id="p", at=NOW)
            elif kind == "repay":
                led.post_repayment(f"r-{i}", tenant, amount, provider_id="p", at=NOW)
            elif kind == "fee":
                led.post_fee(f"f-{i}", tenant, amount, provider_id="p", at=NOW)
        except LedgerError:
            pass  # refused postings change nothing
        led.check_invariants()
        assert not led.cash.is_negative()
        assert all(not r.is_negative() for r in led.receivables().values())
        # The floor binds: a hold only fits above it, settling keeps cash - held unchanged,
        # and releases, repayments and fees only raise it.
        if committed:
            assert led.cash - led.held >= floor


@given(st.lists(st.integers(1, 10_000), min_size=1, max_size=10))
def test_ledger_postings_are_idempotent(amounts):
    led = CapitalLedger(InMemoryKitStore(), usd("0"))
    led.fund("cap", Money.from_minor(sum(amounts), "USD"), at=NOW)
    for i, a in enumerate(amounts):
        m = Money.from_minor(a, "USD")
        for _ in range(2):
            led.place_hold(f"b{i}", "t", m, owner="o")
        for _ in range(2):
            led.post_advance(f"b{i}", "t", m, provider_id="p", at=NOW)
    assert len(led.entries()) == len(amounts) + 1
    assert led.cash == usd("0")
    assert led.receivable("t") == Money.from_minor(sum(amounts), "USD")


class _InterleaveOnLoad(InMemoryKitStore):
    """Runs `interleave` once, at the n-th ledger read from now (inside place_hold)."""

    def __init__(self):
        super().__init__()
        self.interleave = None
        self.after_reads = 0

    def get(self, key):
        if self.interleave is not None:
            if self.after_reads == 0:
                fn, self.interleave = self.interleave, None
                fn()
            else:
                self.after_reads -= 1
        return super().get(key)


def test_a_hold_released_mid_call_is_never_recreated_on_a_stale_wallet_observation():
    """o1 holds X; o2 observes the wallet; then C posts (money out)
    and o1 releases X before o2's write. o2 must not create X against its stale observation."""
    store = _InterleaveOnLoad()
    led = CapitalLedger(store, usd("600"))
    led.fund("cap", usd("1000"), at=NOW)
    led.place_hold("X", "t-a", usd("80"), owner="o1")
    led.place_hold("C", "t-c", usd("80"), owner="oc")
    seen = WalletObservation(led.version, usd("700"))  # wallet 700 when o2 looked

    def meanwhile():
        led.post_advance("C", "t-c", usd("80"), provider_id="tr_c", at=NOW)  # wallet now 620
        led.release_hold("X", owner="o1")

    store.interleave = meanwhile
    store.after_reads = 1  # fires on place_hold's second ledger read, if it makes one
    try:
        hold, created = led.place_hold("X", "t-a", usd("80"), owner="o2", wallet=seen)
    except StaleObservation:
        hold, created = None, False
    store.interleave = None
    # o2 may get o1's existing hold back, or be told to observe again; it must never create a
    # hold of its own on the stale 700 (the wallet is 620: 620 - 80 would breach the 600 floor).
    assert not created
    assert hold is None or hold.owner == "o1"
    assert all(h.owner != "o2" for h in led.holds())


def test_an_existing_hold_is_returned_whatever_the_observation():
    led = CapitalLedger(InMemoryKitStore(), usd("600"))
    led.fund("cap", usd("1000"), at=NOW)
    led.place_hold("X", "t-a", usd("80"), owner="o1")
    stale = WalletObservation(led.version - 1, usd("0"))
    hold, created = led.place_hold("X", "t-a", usd("80"), owner="o2", wallet=stale)
    assert (hold.owner, created) == ("o1", False)
