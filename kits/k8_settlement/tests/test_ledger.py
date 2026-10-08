import pytest
from awx_common import CurrencyMismatch, Money, canonical_json
from awx_k8_settlement.ledger import (
    CycleInput,
    LedgerInvariantError,
    SellerLedger,
    platform_residual,
    settle_cycle,
)
from hypothesis import given
from hypothesis import strategies as st


def usd(x):
    return Money.of(x, "USD")


def cycle(e, f, t, cid="c1"):
    return CycleInput(cid, usd(e), usd(f), usd(t))


def test_first_cycle_retains_reserve_and_pays_the_rest():
    s, led = settle_cycle(SellerLedger.open("bob", "USD"), cycle(8000, 240, 800))
    assert (s.payout, s.retained, s.reserve_after) == (usd(6960), usd(800), usd(800))
    assert s.residual().is_zero()
    assert led.residual().is_zero()
    assert led.payout_pending == usd(6960)
    assert led.version == 1


def test_lower_target_releases_reserve_into_the_payout():
    _, led = settle_cycle(SellerLedger.open("a", "USD"), cycle(1000, 0, 300))
    s, led = settle_cycle(led, cycle(1000, 0, 100, "c2"))
    assert s.released == usd(200)
    assert s.payout == usd(1200)
    assert led.residual().is_zero()


def test_refunds_beyond_cash_draw_reserve_then_become_a_receivable():
    _, led = settle_cycle(SellerLedger.open("bob", "USD"), cycle(8000, 0, 3000))
    s, led = settle_cycle(led, cycle(500, 4500, 50, "c2"))
    assert s.drawn == usd(3000)
    assert s.shortfall == usd(1000)
    assert s.payout == usd(0)
    assert s.unmet_reserve == usd(50)
    assert led.receivable == usd(1000)
    assert led.reserve_held == usd(0)
    assert s.residual().is_zero()
    assert led.residual().is_zero()


def test_receivable_is_netted_against_the_next_payout_only_when_enabled():
    _, led = settle_cycle(SellerLedger.open("bob", "USD"), cycle(0, 100, 0))
    assert led.receivable == usd(100)
    s, netted = settle_cycle(led, cycle(1000, 0, 0, "c2"))
    assert s.receivable_offset == usd(100)
    assert s.payout == usd(900)
    assert netted.receivable.is_zero()
    s2, kept = settle_cycle(led, cycle(1000, 0, 0, "c2"), net_receivables=False)
    assert s2.payout == usd(1000)
    assert kept.receivable == usd(100)


def test_payout_outcomes_and_failed_payout_is_re_sent_next_cycle():
    s, led = settle_cycle(SellerLedger.open("a", "USD"), cycle(1000, 0, 100))
    failed = led.payout_failed(s.payout)
    assert failed.payable == usd(900)
    assert failed.residual().is_zero()
    s2, led2 = settle_cycle(failed, cycle(0, 0, 100, "c2"))
    assert s2.payable_before == usd(900)
    assert s2.payout == usd(900)
    assert led2.payable.is_zero()
    settled = led2.payout_settled(usd(900))
    assert settled.paid_out == usd(900)
    assert settled.residual().is_zero()
    with pytest.raises(LedgerInvariantError):
        settled.payout_settled(usd(1))


def test_recovery_cannot_exceed_receivable():
    _, led = settle_cycle(SellerLedger.open("bob", "USD"), cycle(0, 100, 0))
    assert led.recovery_settled(usd(100)).recovered == usd(100)
    with pytest.raises(LedgerInvariantError):
        led.recovery_settled(usd("100.01"))


def test_currency_mismatch_and_negative_amounts_refused():
    with pytest.raises(CurrencyMismatch):
        settle_cycle(SellerLedger.open("a", "EUR"), cycle(1, 0, 0))
    with pytest.raises(ValueError, match="non-negative"):
        cycle(-1, 0, 0)


def test_platform_residual_names_unassigned_cash():
    _, a = settle_cycle(SellerLedger.open("a", "USD"), cycle(1000, 0, 100))
    _, b = settle_cycle(SellerLedger.open("b", "USD"), cycle(0, 50, 0))
    # a's 900 left at create; the platform holds a's 100 reserve and is owed 50 by b
    cash = usd(100) - usd(50)
    assert platform_residual([a, b], cash, platform_own_cash=usd(0)).is_zero()
    assert platform_residual([a, b], cash + usd(1), platform_own_cash=usd(0)) == usd(1)


# Properties ----------------------------------------------------------------------------------

amounts = st.integers(min_value=0, max_value=10**8).map(lambda m: Money.from_minor(m, "USD"))
cycles = st.tuples(amounts, amounts, amounts)
outcomes = st.sampled_from(["settle", "fail", "pending"])


@st.composite
def histories(draw):
    led = SellerLedger.open("s", "USD")
    steps = draw(
        st.lists(st.tuples(cycles, outcomes, st.booleans(), st.integers(0, 100)), max_size=8)
    )
    out = []
    for i, ((e, f, t), outcome, net, pct) in enumerate(steps):
        s, led = settle_cycle(led, CycleInput(f"c{i}", e, f, t), net_receivables=net)
        out.append(s)
        if outcome == "settle":
            led = led.payout_settled(s.payout)
        elif outcome == "fail":
            led = led.payout_failed(s.payout)
        if not led.receivable.is_zero():
            part = led.receivable.allocate([pct, 100 - pct])[0] if pct < 100 else led.receivable
            led = led.recovery_settled(part)
    return out, led


@given(histories())
def test_cycle_and_seller_identities_hold_for_every_generated_history(history):
    settlements, ledger = history
    for s in settlements:
        assert s.residual().is_zero()
    assert ledger.residual().is_zero()


@given(histories())
def test_reserve_and_receivable_never_go_negative(history):
    settlements, ledger = history
    for s in settlements:
        for name in (
            "payout",
            "reserve_after",
            "shortfall",
            "drawn",
            "released",
            "retained",
            "unmet_reserve",
            "receivable_offset",
        ):
            assert not getattr(s, name).is_negative(), name
    assert not ledger.reserve_held.is_negative()
    assert not ledger.receivable.is_negative()


@given(st.lists(cycles, min_size=2, max_size=5), st.integers(0, 4), cycles)
def test_recalculating_one_seller_never_changes_another(base, which, changed):
    sellers = [f"s{i}" for i in range(len(base))]
    which %= len(base)
    book = {s: SellerLedger.open(s, "USD") for s in sellers}
    before = {s: canonical_json(book[s]) for s in sellers}
    e, f, t = changed
    _, new = settle_cycle(book[sellers[which]], CycleInput("c1", e, f, t))
    book = {**book, sellers[which]: new}
    for s in sellers:
        if s != sellers[which]:
            assert canonical_json(book[s]) == before[s]


@given(cycles, amounts)
def test_payout_falls_by_exactly_the_additional_reserve_when_cash_covers_it(c, extra):
    e, f, t = c
    if e < f:
        return
    base, _ = settle_cycle(SellerLedger.open("s", "USD"), CycleInput("c", e, f, t))
    if extra > base.payout:
        return
    raised, _ = settle_cycle(SellerLedger.open("s", "USD"), CycleInput("c", e, f, t + extra))
    assert base.payout - raised.payout == extra


def test_late_found_payout_clears_payable_or_becomes_an_overpayment_receivable():
    s, led = settle_cycle(SellerLedger.open("a", "USD"), cycle(1000, 0, 100))
    failed = led.payout_failed(s.payout)
    late = failed.payout_found_after_failed(s.payout)
    assert late.payable.is_zero()
    assert late.paid_out == usd(900)
    assert late.residual().is_zero()
    _, resent = settle_cycle(failed, cycle(0, 0, 100, "c2"))  # payable re-sent in c2
    resent = resent.payout_settled(usd(900))
    over = resent.payout_found_after_failed(usd(900))  # ...then c1's surfaced too
    assert over.receivable == usd(900)
    assert over.residual().is_zero()


def test_late_found_recovery_turns_over_collection_into_payable():
    _, led = settle_cycle(SellerLedger.open("b", "USD"), cycle(0, 100, 0))
    led = led.recovery_settled(usd(100))
    twice = led.recovery_found_after_failed(usd(100))
    assert twice.payable == usd(100)
    assert twice.receivable.is_zero()
    assert twice.residual().is_zero()


def oracle(ledger, e, f, t, net):
    """Closed-form, written independently of settle_cycle's step-by-step code."""
    available = e + ledger.payable + ledger.reserve_held - f
    zero = Money.zero("USD")
    if available.is_negative():
        return {"payout": zero, "reserve_after": zero, "receivable": ledger.receivable - available}
    reserve = t if t <= available else available
    rest = available - reserve
    offset = (ledger.receivable if ledger.receivable <= rest else rest) if net else zero
    return {
        "payout": rest - offset,
        "reserve_after": reserve,
        "receivable": ledger.receivable - offset,
    }


@given(histories(), cycles, st.booleans())
def test_settle_cycle_agrees_with_an_independent_closed_form(history, c, net):
    """The cycle identity alone is tautological, so check it against an independent formula."""
    _, ledger = history
    e, f, t = c
    expected = oracle(ledger, e, f, t, net)
    s, new = settle_cycle(ledger, CycleInput("next", e, f, t), net_receivables=net)
    assert s.payout == expected["payout"]
    assert new.reserve_held == expected["reserve_after"]
    assert new.receivable == expected["receivable"]
    assert s.reserve_after == s.reserve_before + s.retained - s.released - s.drawn
    assert new.receivable - ledger.receivable == s.shortfall - s.receivable_offset
    assert new.payout_pending - ledger.payout_pending == s.payout
