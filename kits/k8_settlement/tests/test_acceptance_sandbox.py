"""Kit 8 acceptance scenario (the guide's kit 8 "Required acceptance test"), as a sandbox test.
As committed it runs against FakeAirwallex; a live run swaps the fake for the real sandbox
client.

Rules for every sandbox run:
- Every object carries a `poc-i8-` label.
- The sellers' connected accounts are created once and found again by label.
- Every run gets a fresh book id (M2).
- The whole run holds `flock /tmp/awx-sandbox.lock`.
- At the end nothing is left in flight: no payout or charge NEW or PENDING. Kit 8 creates no
  transfers or cards, so nothing can be left at SENT and no authorisation can be left pending.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from decimal import Decimal

import pytest
from awx_common import Money, ReadRequest
from awx_k8_settlement import store as st

pytestmark = pytest.mark.sandbox

LOCK = "/tmp/awx-sandbox.lock"  # noqa: S108  shared with every kit's live run
LABEL = "poc-i8-seller-"


@contextmanager
def sandbox_lock():
    with open(LOCK, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def find_or_create_sellers(market, sellers) -> dict[str, str]:
    """Connected accounts cannot be deleted, so reuse the kit's by label. On the fake a
    missing one is created; against the sandbox it would be created through the spine
    (accounts/create, submit, simulation activate), which needs an operator's decision first."""
    body = market.port.read(ReadRequest(path="/api/v1/accounts"))
    found = {
        a["nickname"]: a["id"]
        for a in body.get("items", [])
        if str(a.get("nickname", "")).startswith(LABEL)
    }
    out = {}
    for s in sellers:
        name = LABEL + s
        if name not in found:
            found[name] = market.fake.add_connected_account(name, {"USD": Money.of(0, "USD")})
        out[s] = found[name]
    return out


def test_acceptance_carrier_failure_for_seller_x(h):
    with sandbox_lock():
        m = h.Market(h.c1_net(), seed=False)
        m.accounts = find_or_create_sellers(m, h.SELLERS)
        h.seed_through_commands(m, m.stores.__getitem__)  # the deploy path; fresh book ids (M2)
        books = {s: st.book_id(m.stores[s]) for s in h.SELLERS}
        assert len(set(books.values())) == 3
        # Carrier failure for seller X (bob): only X's reserve changes.
        others = {s: h.dump(m.stores[s]) for s in ("alice", "cara")}
        recalc = m.run(h.RECALC, {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"}, "bob")
        assert recalc.state == "SUCCEEDED", recalc.detail
        assert {s: h.dump(m.stores[s]) for s in ("alice", "cara")} == others
        for s in h.SELLERS:
            out = m.run(h.SETTLE, {"cycle_id": "c1"}, s)
            assert out.state == "SUCCEEDED", (s, out.detail, out.postcondition)
            assert m.settled(s, "c1").residual().is_zero()  # cycle identity balances to zero
        # X's payout fell by exactly the additional reserve (rate reserve 800 -> 3000).
        assert m.ledger("bob").reserve_held == h.usd(3000)
        assert m.settled("bob", "c1").payout == h.usd(8000 - 240 - 3000)
        assert m.balanced()  # platform cash = unreleased reserves
        # Refunds surge past X's cash and reserve: the shortfall becomes a receivable.
        refunded = [
            {
                "order_id": f"bob-{i:03d}",
                "amount": h.money_arg(75),
                "refunded": h.money_arg(75),
                "delivered": False,
            }
            for i in range(40)
        ]
        h.op(
            m,
            m.stores.__getitem__,
            "load_orders",
            {"seller": "bob", "as_of": "2026-10-06T06:00:00Z", "orders": refunded},
        )
        h.load_cycle(m, m.stores.__getitem__, "bob", "c2", 500, 4500, 500)
        assert m.run(h.SETTLE, {"cycle_id": "c2"}, "bob").state == "SUCCEEDED"
        assert m.ledger("bob").receivable == h.usd(1000)
        # X lacks funds: no recovery charge is attempted.
        m.set_wallet("bob", h.usd(0))
        assert m.run(h.RECOVER, {}, "bob").state == "SUCCEEDED"
        assert m.charges_from("bob") == []
        assert m.ledger("bob").receivable == h.usd(1000)
        # Cross-tenant movement is rejected.
        cross = m.run(h.RECALC, {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"}, "alice")
        assert cross.state == "BLOCKED"
        # Every object is labelled, and nothing is left in flight.
        for kind in h.KINDS:
            for o in m.port.objects(kind):
                assert o["reference"].startswith("poc-i8-")
                assert o["status"] not in ("NEW", "PENDING")
        assert m.balanced()


def test_a_second_run_gets_new_request_ids_and_adopts_nothing(h):
    """M2 on a shared account: a re-run seeded the same way reuses the accounts but never the
    first run's payouts."""
    with sandbox_lock():
        first = h.Market(h.c1_net(), seed=False)
        first.accounts = find_or_create_sellers(first, ("bob",))
        h.seed_through_commands(first, first.stores.__getitem__, ("bob",))
        first.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
        second = h.Market(h.usd(0), seed=False)
        second.fake, second.port = first.fake, first.port  # same sandbox, same accounts
        second.fund(h.bob_c1_net())
        second.accounts = find_or_create_sellers(second, ("bob",))
        assert second.accounts["bob"] == first.accounts["bob"]
        h.seed_through_commands(second, second.stores.__getitem__, ("bob",))
        assert st.book_id(second.stores["bob"]) != st.book_id(first.stores["bob"])
        out = second.run(h.SETTLE, {"cycle_id": "c1"}, "bob")
        assert out.state == "SUCCEEDED", out.postcondition
        rids = {o["request_id"] for o in first.transfers_to("bob")}
        assert len(rids) == 2  # two runs, two payouts, no adoption
        assert Decimal(first.wallet("bob").amount) == Decimal(2 * 6960)
