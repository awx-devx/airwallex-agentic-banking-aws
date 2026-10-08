"""Property test through the REAL runner and FakeAirwallex: an oracle built only from the raw
cycle inputs and the money the fake actually moved.

Steps interleave freely:
- settlements whose payout lands, fails, suspends, times out invisibly, is lost, is refused,
  is rate-limited, or arrives late;
- recovery charges with the same outcomes;
- operator resolutions at Airwallex;
- late deliveries, resends of in-flight money, read-only reconciles, and time passing.

After every step, and after a final delivery and reconcile, the oracle checks that:
- no seller is paid more than its settled payouts, nor charged more than it owes;
- money becomes payable again only after a definitive failure;
- the platform's cash is assigned to sellers to the cent.

EXAMPLES reproduce each known double-movement path, so their mutants fail on every seed.
"""

from decimal import Decimal

from awx_common import Money, Origin
from awx_k8_settlement import platform_residual
from awx_k8_settlement import store as st
from awx_k8_settlement.operations import DEFINITIVE_FAILURES, payout_request_id
from awx_spine_aws.engine import MAX_RESENDS
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as stg

USD = "USD"
CAT = "connected_account_transfers.create"
CHARGE = "charges.create"
LANDED = frozenset({"NEW", "PENDING", "SUSPENDED", "SETTLED"})
PAYOUT_MODES = (
    "ok",
    "fail",
    "suspend",
    "timeout",
    "lost",
    "pending",
    "nsf",
    "reject",
    "late",
    "late",
    "ratelimited",
)
CHARGE_MODES = ("ok", "nsf", "reject", "suspend", "late", "late", "lost", "timeout", "ratelimited")


def usd(x):
    return Money.of(x, USD)


def modes(mode):
    """A first send's script. Every in-run resend after an unknown first reply is lost or
    rate-limited, so a refusal at create is only ever seen on a FIRST send here (the in-run
    refused-resend case is in test_spine_faults)."""
    if mode == "late":
        return ("late", *(["lost"] * MAX_RESENDS))
    if mode == "lost":
        return ("lost", *(["lost"] * MAX_RESENDS))
    if mode == "timeout":
        return ("timeout:90", *(["ratelimit"] * (MAX_RESENDS + 2)))
    if mode == "ratelimited":
        return ("timeout:90", *(["ratelimit"] * (MAX_RESENDS + 2)))
    return (mode,)


money = stg.integers(0, 2_000_000).map(lambda m: Money.from_minor(m, USD))
earned = stg.integers(1_000, 2_000_000).map(lambda m: Money.from_minor(m, USD))
refunds = stg.one_of(
    stg.just(usd(0)), stg.integers(0, 200_000).map(lambda m: Money.from_minor(m, USD)), money
)
cycle_step = stg.tuples(
    stg.just("cycle"), stg.integers(0, 2), earned, refunds, money, stg.sampled_from(PAYOUT_MODES)
)
recover_step = stg.tuples(
    stg.just("recover"), stg.integers(0, 2), money, stg.sampled_from(CHARGE_MODES)
)
resolve_step = stg.tuples(
    stg.just("resolve"), stg.integers(0, 2), stg.sampled_from(("SETTLED", "FAILED", "none"))
)
deliver_step = stg.tuples(stg.just("deliver"), stg.integers(0, 2))
resend_step = stg.tuples(
    stg.just("resend"),
    stg.integers(0, 2),
    stg.sampled_from(("ok", "reject", "nsf", "late", "lost")),
)
reconcile_step = stg.tuples(stg.just("reconcile"), stg.integers(0, 2))
wait_step = stg.tuples(stg.just("wait"), stg.integers(0, 2), stg.sampled_from((5, 100, 3600)))


def total(objs, statuses):
    return sum((Money.of(o["amount"], USD) for o in objs if o["status"] in statuses), usd(0))


def check(h, m, inputs):
    unlanded = usd(0)
    ledgers = [m.ledger(s) for s in h.SELLERS]
    for s, led in zip(h.SELLERS, ledgers, strict=True):
        transfers, charges = m.transfers_to(s), m.charges_from(s)
        state = st.ledger_state(m.stores[s], s, USD)
        book = st.book_id(m.stores[s])
        e, f = inputs[s]
        settled = usd(0)
        for key in state.postings:
            if not key.startswith("settle:"):
                continue
            stl = st.settlement(state, key.removeprefix("settle:"))
            settled = settled + stl.payout
            rid = payout_request_id(book, s, stl.cycle_id)
            objs = [o for o in transfers if o["request_id"] == rid]
            assert len(objs) <= 1  # one request_id, one transfer
            if objs:  # what moved is exactly what was settled
                assert Money.of(objs[0]["amount"], USD) == stl.payout
            if st.payout_outcome(state, stl.cycle_id) in DEFINITIVE_FAILURES:
                # M1: payable again only after a definitive failure: nothing landed, and
                # nothing still on its way.
                assert not objs or objs[0]["status"] == "FAILED"
                assert rid not in m.port.deferred
        assert not any(k.startswith("payout-late:") for k in state.postings)
        for a in st.attempts_in(state):
            if a.outcome in ("FAILED", "INSUFFICIENT_FUNDS"):
                objs = [o for o in charges if o["request_id"] == a.request_id]
                assert not objs or objs[0]["status"] == "FAILED"
                assert a.request_id not in m.port.deferred
        landed_out, landed_in = total(transfers, LANDED), total(charges, LANDED)
        assert led.paid_out <= total(transfers, {"SETTLED"})
        assert led.recovered <= total(charges, {"SETTLED"})
        assert landed_out <= led.paid_out + led.payout_pending  # never paid twice
        assert led.paid_out + led.payout_pending + led.payable <= settled
        assert landed_in <= led.recovered + led.receivable  # never charged more than owed
        assert e - f + led.recovered - led.paid_out == (
            led.reserve_held + led.payable + led.payout_pending - led.receivable
        )
        assert not led.reserve_held.is_negative()
        assert not led.receivable.is_negative()
        landed_unposted = landed_out - led.paid_out
        assert led.payout_pending >= landed_unposted
        unlanded = unlanded + led.payout_pending - landed_unposted + landed_in - led.recovered
    residual = platform_residual(ledgers, m.platform_cash, platform_own_cash=usd(0))
    assert residual == unlanded


def apply(h, m, inputs, n, step):
    kind, seller = step[0], h.SELLERS[step[1]]
    if kind == "cycle":
        _, _, e, f, vol, mode = step
        cid = f"p{n}"
        st.put_cycle(m.stores[seller], st.CycleData(cid, e, f, vol))
        if h.SETTLE.policy({"cycle_id": cid}, m.context(seller)).risk_tier.value == "BLOCKED":
            return
        m.fund(e - f)
        ie, if_ = inputs[seller]
        inputs[seller] = (ie + e, if_ + f)
        m.port.script(CAT, *modes(mode))
        m.run(h.SETTLE, {"cycle_id": cid}, seller, origin=Origin.SCHEDULE)
    elif kind == "recover":
        _, _, wallet, mode = step
        m.set_wallet(seller, wallet)
        m.port.script(CHARGE, *modes(mode))
        m.run(h.RECOVER, {}, seller)
    elif kind == "resolve":
        status = step[2]
        if status != "none":
            for kind_ in h.KINDS:
                for o in m.port.objects(kind_, account=m.accounts[seller]):
                    if o["status"] in ("SUSPENDED", "PENDING"):
                        try:
                            m.port.set_status(o["request_id"], status)
                        except ValueError:
                            pass  # the fake refuses a FAILED whose payee spent the money
    elif kind == "deliver":
        for rid, op in list(m.port.deferred.items()):
            if op.body.get("destination", op.body.get("source")) == m.accounts[seller]:
                m.port.deliver(rid)
    elif kind == "resend":
        mode = step[2]
        state = st.ledger_state(m.stores[seller], seller, USD)
        for cid in st.unresolved_payouts(state):
            m.port.script(CAT, *modes(mode))
            m.run(h.SETTLE, {"cycle_id": cid}, seller, origin=Origin.SCHEDULE)
            m.port.clear()
        if any(a.outcome in st.PENDING_RECOVERY for a in st.recovery_attempts(m.stores[seller])):
            m.port.script(CHARGE, *modes(mode))
            m.run(h.RECOVER, {}, seller, origin=Origin.SCHEDULE)
    elif kind == "wait":
        m.advance(step[2])
    else:
        m.run(h.RECONCILE, {}, seller)
    m.port.clear()


EXAMPLES = (
    # An operator FAILED the fake refuses (the payee spent the money):
    # suspend, drain the seller's wallet, then FAILED. The movement stays SUSPENDED.
    [
        ("cycle", 1, usd(1000), usd(0), usd(0), "suspend"),
        ("recover", 1, usd(0), "ok"),
        ("resolve", 1, "FAILED"),
        ("reconcile", 1),
        ("cycle", 1, usd(500), usd(0), usd(0), "ok"),
    ],
    # payout: first send in transit, resend refused at create, then the first copy lands
    [
        ("cycle", 1, usd(1000), usd(0), usd(0), "late"),
        ("resend", 1, "reject"),
        ("deliver", 1),
        ("cycle", 1, usd(1000), usd(0), usd(0), "ok"),
    ],
    [
        ("cycle", 1, usd(1000), usd(0), usd(0), "late"),
        ("resend", 1, "nsf"),
        ("deliver", 1),
        ("cycle", 1, usd(1000), usd(0), usd(0), "ok"),
    ],
    # charge: first send in transit, resend refused for funds, then the first copy lands
    [
        ("cycle", 1, usd(10), usd(100), usd(0), "ok"),
        ("recover", 1, usd(1000), "late"),
        ("resend", 1, "nsf"),
        ("deliver", 1),
        ("recover", 1, usd(1000), "ok"),
    ],
    # netting: a settlement while a charge on the same receivable is in transit
    [
        ("cycle", 1, usd(10), usd(100), usd(0), "ok"),
        ("recover", 1, usd(1000), "late"),
        ("cycle", 1, usd(500), usd(0), usd(0), "ok"),
        ("deliver", 1),
    ],
    # an invisible landing, every resend rate-limited, then visible
    [
        ("cycle", 1, usd(1000), usd(0), usd(0), "ratelimited"),
        ("wait", 1, 100),
        ("cycle", 1, usd(1000), usd(0), usd(0), "ok"),
    ],
    [
        ("cycle", 1, usd(10), usd(100), usd(0), "ok"),
        ("recover", 1, usd(1000), "ratelimited"),
        ("cycle", 1, usd(500), usd(0), usd(0), "ok"),
        ("wait", 1, 100),
    ],
)


def _with_examples(test):
    for case in reversed(EXAMPLES):
        test = example(steps=case)(test)
    return test


@_with_examples
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    steps=stg.lists(
        stg.one_of(
            cycle_step,
            cycle_step,
            cycle_step,
            recover_step,
            recover_step,
            resolve_step,
            resolve_step,
            deliver_step,
            resend_step,
            resend_step,
            reconcile_step,
            wait_step,
        ),
        min_size=4,
        max_size=14,
    )
)
def test_real_runner_agrees_with_an_oracle_from_raw_inputs_and_moved_money(h, steps):
    m = h.Market(usd(0), seed=False)
    for s, (days, rate) in h.PROFILES.items():
        st.seed_seller(
            m.stores[s], st.SellerProfile(s, USD, days, Decimal(rate)), account=m.accounts[s]
        )
    inputs = {s: (usd(0), usd(0)) for s in h.SELLERS}
    for n, step in enumerate(steps):
        apply(h, m, inputs, n, step)
        check(h, m, inputs)
    if m.port.deferred:
        event("late requests delivered at the end")
    m.port.deliver()
    m.advance(100)
    for s in h.SELLERS:
        m.run(h.RECONCILE, {}, s)
    check(h, m, inputs)
    if m.port.objects("connected_account_transfers"):
        event("money moved")
