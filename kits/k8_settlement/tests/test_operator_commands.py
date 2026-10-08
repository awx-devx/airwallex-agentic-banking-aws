"""Operator commands for kit 8: a deployed kit gets its seller state only through
OPERATOR_COMMANDS, and that is enough to run every shipped TOOL and the acceptance SPECS end
to end.

The end-to-end test starts from EMPTY KitToolHarness stores, seeds and loads data only through
run_operator_command (no conftest seeding), then drives every tool through the real dispatcher
and runs the acceptance actions through the real runner on the same stores.
"""

import pytest
from awx_agent.testing import KitToolHarness
from awx_common.operator_commands import OperatorCommandError, check_operator_commands
from awx_k8_settlement import OPERATOR_COMMANDS, SPECS, TOOLS
from awx_k8_settlement import store as st


class Deployed:
    """A Market whose seller stores ARE the harness's stores, seeded only by commands."""

    def __init__(self, h, platform_cash) -> None:
        self.m = h.Market(platform_cash, seed=False)
        self.harness = KitToolHarness.for_kit(
            "k8", port=self.m.port, tenants=dict(self.m.accounts), clock=self.m.now
        )
        assert all(not self.harness.store(s).query("") for s in h.SELLERS)  # empty
        self.m.stores = {s: self.harness.store(s) for s in h.SELLERS}
        self.m.op_platform = self.harness.store()  # the kit's platform partition
        self.store_for = self.harness.store

    def op(self, h, name, args, **kw):
        return h.op(self.m, self.store_for, name, args, **kw)

    def call(self, tool, args, tenant):
        token = self.harness.issuer.token(tenant_id=tenant, now=self.m.now().timestamp())
        return self.harness.call(tool, args, token=token)


def seed_args(h, m, seller="bob", **over):
    days, rate = h.PROFILES[seller]
    return {
        "seller": seller,
        "connected_account_id": m.accounts[seller],
        "currency": "USD",
        "tenure_days": days,
        "trailing_refund_rate": rate,
        **over,
    }


def test_the_commands_pass_the_contract_check():
    check_operator_commands(
        OPERATOR_COMMANDS, tool_names=[t.name for t in TOOLS], actions=list(SPECS)
    )
    assert [c.name for c in OPERATOR_COMMANDS if c.once] == ["seed_seller"]
    assert {c.name for c in OPERATOR_COMMANDS} == {
        "seed_seller",
        "load_cycle",
        "load_orders",
        "load_carrier_events",
    }


def test_every_tool_and_the_acceptance_run_on_command_seeded_stores(h):
    d = Deployed(h, h.c1_net())
    h.seed_through_commands(d.m, d.store_for)
    m = d.m
    # --- every shipped TOOL through the real dispatcher ------------------------------------
    ledger = d.call("read_seller_ledger", {}, "bob")["untrusted_data"]
    assert ledger["identity_residual"]["amount"] == "0.00"
    m.set_wallet("bob", h.usd(42))
    bal = d.call("read_seller_balance", {"currency": "USD"}, "bob")["untrusted_data"]
    assert bal["available"]["amount"] == "42.00"
    view = d.call("read_carrier_event", {"carrier_event_id": "evt-bob-1"}, "bob")
    assert view["untrusted_data"]["exposure_computed_by_code"]["amount"] == "3000.00"
    out = {
        "propose_settle_seller_cycle": d.call(
            "propose_settle_seller_cycle", {"cycle_id": "c1"}, "bob"
        ),
        "propose_recalculate_seller_reserve": d.call(
            "propose_recalculate_seller_reserve",
            {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"},
            "bob",
        ),
        "propose_reconcile_seller_money": d.call("propose_reconcile_seller_money", {}, "bob"),
        "propose_recover_shortfall": d.call("propose_recover_shortfall", {}, "bob"),
        "propose_resolve_in_flight_money": d.call(
            "propose_resolve_in_flight_money",
            {
                "kind": "payout",
                "ref": "c1",
                "resolution": "not_landed",
                "evidence_ref": "awx-case:1",
            },
            "bob",
        ),
    }
    assert set(out) == {t.name for t in TOOLS if t.name.startswith("propose_")}
    assert out["propose_settle_seller_cycle"]["status"] == "AWAITING_APPROVAL"
    assert out["propose_recalculate_seller_reserve"]["status"] == "AWAITING_APPROVAL"
    for tool in (
        "propose_reconcile_seller_money",
        "propose_recover_shortfall",
        "propose_resolve_in_flight_money",
    ):  # nothing to do yet: refused, not broken
        assert out[tool]["status"] == "BLOCKED_BY_POLICY", out[tool]
    assert "seed" not in str(out)  # no tool fails for want of seeded state
    # --- the acceptance SPECS through the real runner, on the same stores --------------------
    recalc = m.run(h.RECALC, {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"}, "bob")
    assert recalc.state == "SUCCEEDED", recalc.detail
    for s in h.SELLERS:
        o = m.run(h.SETTLE, {"cycle_id": "c1"}, s)
        assert o.state == "SUCCEEDED", (s, o.detail, o.postcondition)
    assert m.ledger("bob").reserve_held == h.usd(3000)
    assert m.balanced()
    h.load_cycle(m, d.store_for, "bob", "c2", 500, 4500, 500)  # command, then buyer money
    rows = [
        {
            "order_id": f"bob-{i:03d}",
            "amount": h.money_arg(75),
            "refunded": h.money_arg(75),
            "delivered": False,
        }
        for i in range(40)
    ]
    d.op(h, "load_orders", {"seller": "bob", "as_of": "2026-10-06T06:00:00Z", "orders": rows})
    assert m.run(h.SETTLE, {"cycle_id": "c2"}, "bob").state == "SUCCEEDED"
    assert m.ledger("bob").receivable == h.usd(1000)
    m.set_wallet("bob", h.usd(0))
    assert m.run(h.RECOVER, {}, "bob").state == "SUCCEEDED"
    assert m.charges_from("bob") == []
    assert m.balanced()
    # the tools see the state the runner built, on the very same stores
    after = d.call("read_seller_ledger", {}, "bob")["untrusted_data"]
    assert after["receivable"]["amount"] == "1000.00"


def test_seed_twice_is_refused_and_replace_keeps_the_book_id(h):
    d = Deployed(h, h.usd(0))
    first = d.op(h, "seed_seller", seed_args(h, d.m))
    with pytest.raises(OperatorCommandError, match="already seeded"):
        d.op(h, "seed_seller", seed_args(h, d.m))
    again = d.op(h, "seed_seller", seed_args(h, d.m, tenure_days=400, replace=True))
    assert again["book_id"] == first["book_id"]
    assert again["replaced"] is True
    assert st.profile(d.store_for("bob")).tenure_days == 400
    assert st.book_id(d.store_for("bob")) == first["book_id"]
    # the book id is minted, not a constant: another seller (or deploy) gets another one
    other = d.op(h, "seed_seller", seed_args(h, d.m, seller="cara"))
    assert other["book_id"] != first["book_id"]
    with pytest.raises(OperatorCommandError, match="currency"):
        d.op(h, "seed_seller", seed_args(h, d.m, currency="EUR", replace=True))


def test_feeds_are_idempotent_per_natural_key(h):
    d = Deployed(h, h.usd(0))
    d.op(h, "seed_seller", seed_args(h, d.m))
    cycle = {
        "seller": "bob",
        "cycle_id": "c9",
        "entitlement": h.money_arg(100),
        "refunds": h.money_arg(0),
        "volume_base": h.money_arg(100),
    }
    assert d.op(h, "load_cycle", cycle)["status"] == "loaded"
    assert d.op(h, "load_cycle", cycle)["status"] == "unchanged"
    with pytest.raises(OperatorCommandError, match="different content"):
        d.op(h, "load_cycle", {**cycle, "entitlement": h.money_arg(101)})
    ev = {**h.BOB_EVENT, "event_id": "evt-9"}
    assert d.op(h, "load_carrier_events", {"seller": "bob", "events": [ev]})["loaded"] == ["evt-9"]
    assert d.op(h, "load_carrier_events", {"seller": "bob", "events": [ev]})["unchanged"] == [
        "evt-9"
    ]
    with pytest.raises(OperatorCommandError, match="different content"):
        d.op(
            h, "load_carrier_events", {"seller": "bob", "events": [{**ev, "narrative": "changed"}]}
        )
    row = {
        "order_id": "o-1",
        "amount": h.money_arg(50),
        "refunded": h.money_arg(0),
        "delivered": False,
    }
    t0, t1 = "2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z"
    assert d.op(h, "load_orders", {"seller": "bob", "as_of": t0, "orders": [row]})["loaded"] == 1
    assert d.op(h, "load_orders", {"seller": "bob", "as_of": t0, "orders": [row]})["unchanged"] == 1
    with pytest.raises(OperatorCommandError, match="different content"):
        d.op(
            h, "load_orders", {"seller": "bob", "as_of": t0, "orders": [{**row, "delivered": True}]}
        )
    upd = d.op(
        h, "load_orders", {"seller": "bob", "as_of": t1, "orders": [{**row, "delivered": True}]}
    )
    assert upd["updated"] == 1
    assert st.orders(d.store_for("bob"), "bob")[0].delivered is True
    with pytest.raises(OperatorCommandError, match="newer row"):
        d.op(h, "load_orders", {"seller": "bob", "as_of": t0, "orders": [row]})


@pytest.mark.parametrize(
    "account", ["acct_PLACEHOLDER0001", "acct_<seller>", "poc-i8-bob", "acct_example123456"]
)
def test_placeholder_ids_are_refused(h, account):
    d = Deployed(h, h.usd(0))
    with pytest.raises(OperatorCommandError, match="not a real connected account"):
        d.op(h, "seed_seller", seed_args(h, d.m, connected_account_id=account))
    assert d.store_for("bob").query("") == []


def test_the_account_must_exist_and_carry_the_poc_i8_label(h):
    d = Deployed(h, h.usd(0))
    with pytest.raises(OperatorCommandError, match="has no account"):
        d.op(h, "seed_seller", seed_args(h, d.m, connected_account_id="acct_AbCdEf123456"))
    foreign = d.m.fake.add_connected_account("someone-else", {"USD": h.usd(0)})
    with pytest.raises(OperatorCommandError, match="not labelled poc-i8-"):
        d.op(h, "seed_seller", seed_args(h, d.m, connected_account_id=foreign))
    with pytest.raises(OperatorCommandError, match="online"):
        d.op(h, "seed_seller", seed_args(h, d.m), read=False)
    assert d.store_for("bob").query("") == []


def test_feeds_need_a_seeded_seller_and_refuse_misfiled_evidence(h):
    d = Deployed(h, h.usd(0))
    with pytest.raises(OperatorCommandError, match="seed the seller first"):
        d.op(
            h,
            "load_cycle",
            {
                "seller": "bob",
                "cycle_id": "c1",
                "entitlement": h.money_arg(1),
                "refunds": h.money_arg(0),
                "volume_base": h.money_arg(1),
            },
        )
    d.op(h, "seed_seller", seed_args(h, d.m, seller="alice"))
    with pytest.raises(OperatorCommandError, match="names seller bob"):
        d.op(h, "load_carrier_events", {"seller": "alice", "events": [h.BOB_EVENT]})
    with pytest.raises(OperatorCommandError, match="decimal string"):
        d.op(
            h,
            "load_cycle",
            {
                "seller": "alice",
                "cycle_id": "c1",
                "entitlement": {"amount": 1.5, "currency": "USD"},
                "refunds": h.money_arg(0),
                "volume_base": h.money_arg(1),
            },
        )


def test_commands_move_no_money_and_run_no_workflow(h):
    d = Deployed(h, h.c1_net())
    before = d.m.platform_cash
    h.seed_through_commands(d.m, d.store_for)
    assert d.harness.sfn.calls == []
    assert d.m.platform_cash == before
    for kind in h.KINDS:
        assert d.m.port.objects(kind) == []
    assert all(d.m.wallet(s).is_zero() for s in h.SELLERS)


# --- one account per seller; money only to the seeded account ------------------------------


def _nothing_moved(h, m):
    assert m.port.executed == []
    for kind in h.KINDS:
        assert m.port.objects(kind) == []


def test_a_second_seller_on_a_bound_account_is_refused_at_seed(h):
    d = Deployed(h, h.usd(0))
    d.op(h, "seed_seller", seed_args(h, d.m, seller="alice"))
    before = {s: h.dump(d.store_for(s)) for s in h.SELLERS}
    with pytest.raises(OperatorCommandError, match="already bound to seller alice"):
        d.op(h, "seed_seller", seed_args(h, d.m, connected_account_id=d.m.accounts["alice"]))
    assert {s: h.dump(d.store_for(s)) for s in h.SELLERS} == before  # bob: nothing written
    again = d.op(h, "seed_seller", seed_args(h, d.m, seller="alice", replace=True))
    assert again["replaced"] is True  # same seller, same account: not a conflict
    _nothing_moved(h, d.m)


def test_a_replace_onto_another_account_is_refused(h):
    d = Deployed(h, h.usd(0))
    d.op(h, "seed_seller", seed_args(h, d.m))
    spare = d.m.fake.add_connected_account("poc-i8-seller-spare", {"USD": h.usd(0)})
    before = h.dump(d.store_for("bob"))
    with pytest.raises(OperatorCommandError, match="never moves a seller"):
        d.op(h, "seed_seller", seed_args(h, d.m, connected_account_id=spare, replace=True))
    assert h.dump(d.store_for("bob")) == before
    assert d.harness.store().get("seller-account/" + spare) is None
    assert st.seeded_account(d.store_for("bob")) == d.m.accounts["bob"]
    _nothing_moved(h, d.m)


def test_a_tenants_row_pointing_at_another_sellers_account_blocks_every_money_action(h):
    """bob is seeded on his own account, but his tenants row points at alice's.
    settle, recover and resolve all BLOCK on the runner and through the tools; nothing moves."""
    d = Deployed(h, h.c1_net())
    h.seed_through_commands(d.m, d.store_for)
    m = d.m
    m.set_wallet("bob", h.usd(1000))
    m.accounts["bob"] = m.accounts["alice"]  # the tenants row, wrong
    resolve = {
        "kind": "payout",
        "ref": "c1",
        "resolution": "not_landed",
        "evidence_ref": "awx-case:1",
    }
    for spec, raw in ((h.SETTLE, {"cycle_id": "c1"}), (h.RECOVER, {}), (h.RESOLVE, resolve)):
        out = m.run(spec, raw, "bob")
        assert out.state == "BLOCKED", (spec, out.state, out.detail)
        assert "the tenants table and the seeded account disagree" in out.proposal.reasons[0]
    harness = KitToolHarness.for_kit("k8", port=m.port, tenants=dict(m.accounts), clock=m.now)
    harness.stores.update(d.harness.stores)  # the same seeded stores, the wrong tenants row
    token = harness.issuer.token(tenant_id="bob", now=m.now().timestamp())
    for tool, args in (
        ("propose_settle_seller_cycle", {"cycle_id": "c1"}),
        ("propose_recover_shortfall", {}),
        ("propose_resolve_in_flight_money", resolve),
    ):
        out = harness.call(tool, args, token=token)
        assert out["status"] == "BLOCKED_BY_POLICY", out
        assert "disagree" in str(out)
    _nothing_moved(h, m)
    assert m.wallet("alice").is_zero()


def test_an_account_that_is_not_active_is_refused(h):
    """The fake's accounts are ACTIVE; a reader answering SUSPENDED
    for bob's account is refused and nothing is written."""
    d = Deployed(h, h.usd(0))

    def suspended(req):
        return {**d.m.fake.read(req), "status": "SUSPENDED"}

    with pytest.raises(OperatorCommandError, match="SUSPENDED, not ACTIVE"):
        d.op(h, "seed_seller", seed_args(h, d.m), read=suspended)
    assert d.store_for("bob").query("") == []
    assert d.harness.store().query("") == []


# --- a refused batch writes nothing ----------------------------------------------------------


def _row(h, oid, delivered=False, refunded=0):
    return {
        "order_id": oid,
        "amount": h.money_arg(50),
        "refunded": h.money_arg(refunded),
        "delivered": delivered,
    }


def test_a_mixed_stale_order_batch_writes_nothing_and_the_good_row_retries(h):
    d = Deployed(h, h.usd(0))
    d.op(h, "seed_seller", seed_args(h, d.m))
    t0, t1 = "2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z"
    d.op(h, "load_orders", {"seller": "bob", "as_of": t1, "orders": [_row(h, "o-old")]})
    before = h.dump(d.store_for("bob"))
    with pytest.raises(OperatorCommandError, match="newer row"):  # o-new fine, o-old stale
        d.op(
            h,
            "load_orders",
            {"seller": "bob", "as_of": t0, "orders": [_row(h, "o-new"), _row(h, "o-old", True)]},
        )
    assert h.dump(d.store_for("bob")) == before
    assert {o.order_id for o in st.orders(d.store_for("bob"), "bob")} == {"o-old"}
    out = d.op(h, "load_orders", {"seller": "bob", "as_of": t0, "orders": [_row(h, "o-new")]})
    assert out["loaded"] == 1
    assert {o.order_id for o in st.orders(d.store_for("bob"), "bob")} == {"o-old", "o-new"}
    assert [i.key for i in d.store_for("bob").query("feed/")] == []  # stamps live in "orders"


def test_an_order_batch_with_a_duplicate_row_is_refused_cleanly(h):
    d = Deployed(h, h.usd(0))
    d.op(h, "seed_seller", seed_args(h, d.m))
    before = h.dump(d.store_for("bob"))
    with pytest.raises(OperatorCommandError, match="appears twice"):
        d.op(
            h,
            "load_orders",
            {
                "seller": "bob",
                "as_of": "2026-10-06T00:00:00Z",
                "orders": [_row(h, "o-1"), _row(h, "o-2"), _row(h, "o-1", True)],
            },
        )
    assert h.dump(d.store_for("bob")) == before


def test_a_refused_carrier_event_batch_writes_nothing(h):
    d = Deployed(h, h.usd(0))
    d.op(h, "seed_seller", seed_args(h, d.m))
    good = {**h.BOB_EVENT, "event_id": "evt-ok"}
    for bad, match in (
        ({**h.BOB_EVENT, "event_id": "evt-x", "seller": "alice"}, "names seller"),
        (good, "appears twice"),
    ):
        before = h.dump(d.store_for("bob"))
        with pytest.raises(OperatorCommandError, match=match):
            d.op(h, "load_carrier_events", {"seller": "bob", "events": [good, bad]})
        assert h.dump(d.store_for("bob")) == before
    d.op(h, "load_carrier_events", {"seller": "bob", "events": [good]})
    before = h.dump(d.store_for("bob"))
    with pytest.raises(OperatorCommandError, match="different content"):
        d.op(
            h,
            "load_carrier_events",
            {
                "seller": "bob",
                "events": [
                    {**h.BOB_EVENT, "event_id": "evt-new"},
                    {**good, "narrative": "changed"},
                ],
            },
        )
    assert h.dump(d.store_for("bob")) == before  # evt-new was not written either


# --- a seller never sees another seller's account id or money -------------------------------


def test_under_a_stale_tenants_row_no_tool_shows_the_other_sellers_account_or_money(h):
    """bob's tenants row points at alice's seeded account and alice's wallet holds 4321. All 8
    tools answer with the fixed reason only: no account id of alice's, no figure of hers, no
    Airwallex read of her account, nothing sent."""
    d = Deployed(h, h.c1_net())
    h.seed_through_commands(d.m, d.store_for)
    m = d.m
    alice = m.accounts["alice"]
    m.set_wallet("alice", h.usd(4321))
    m.accounts["bob"] = alice  # the stale tenants row
    harness = KitToolHarness.for_kit("k8", port=m.port, tenants=dict(m.accounts), clock=m.now)
    harness.stores.update(d.harness.stores)
    token = harness.issuer.token(tenant_id="bob", now=m.now().timestamp())
    reads_before = len(m.port.reads)
    args = {
        "read_seller_balance": {"currency": "USD"},
        "read_seller_ledger": {},
        "read_carrier_event": {"carrier_event_id": "evt-bob-1"},
        "propose_settle_seller_cycle": {"cycle_id": "c1"},
        "propose_recalculate_seller_reserve": {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"},
        "propose_reconcile_seller_money": {},
        "propose_recover_shortfall": {},
        "propose_resolve_in_flight_money": {
            "kind": "payout",
            "ref": "c1",
            "resolution": "not_landed",
            "evidence_ref": "awx-case:1",
        },
    }
    assert set(args) == {t.name for t in TOOLS}
    for tool, a in args.items():
        out = harness.call(tool, a, token=token)
        text = repr(out)
        assert "the tenants table and the seeded account disagree" in text, (tool, out)
        assert alice not in text, (tool, out)
        assert "4321" not in text, (tool, out)
        assert "untrusted_data" not in out, (tool, out)  # no figures at all
    for r in m.port.reads[reads_before:]:
        assert alice not in repr(r), r
    _nothing_moved(h, m)
    assert m.wallet("alice") == h.usd(4321)


def test_the_block_reason_from_the_runner_names_no_account_id(h):
    d = Deployed(h, h.c1_net())
    h.seed_through_commands(d.m, d.store_for)
    m = d.m
    ids = {m.accounts["bob"], m.accounts["alice"]}
    m.accounts["bob"] = m.accounts["alice"]
    for spec, raw in (
        (h.SETTLE, {"cycle_id": "c1"}),
        (h.RECOVER, {}),
        (h.RECONCILE, {}),
        (h.RECALC, {"cycle_id": "c1", "carrier_event_id": "evt-bob-1"}),
    ):
        out = m.run(spec, raw, "bob")
        assert out.state == "BLOCKED"
        assert out.proposal.reasons == ("the tenants table and the seeded account disagree",)
        assert not any(i in repr(out) for i in ids)
    _nothing_moved(h, m)


# --- the account index is one item; a lost race leaves nothing ------------------------------


class RaceStore:
    """Wraps the platform store: just before the first write of the account index (whatever
    its key), run `other` (a competing seed) to completion."""

    def __init__(self, inner, other) -> None:
        self.inner, self.other = inner, other

    def get(self, key):
        return self.inner.get(key)

    def query(self, prefix):
        return self.inner.query(prefix)

    def put(self, key, value, *, expected_version):
        if self.other is not None and key.startswith("seller-account"):
            other, self.other = self.other, None
            other()
        return self.inner.put(key, value, expected_version=expected_version)


def _index(d):
    out = {}
    for item in d.harness.store().query("seller-account"):
        if "accounts" in item.value:
            out.update(item.value["accounts"])
        else:  # e05f8df's per-account entries
            out[item.key.removeprefix("seller-account/")] = item.value["seller"]
    return out


def test_two_concurrent_seeds_of_one_seller_on_two_accounts_bind_exactly_one(h):
    d = Deployed(h, h.usd(0))
    spare = d.m.fake.add_connected_account("poc-i8-seller-bob2", {"USD": h.usd(0)})
    platform = d.harness.store()
    winner = {}

    def other():
        winner.update(d.op(h, "seed_seller", seed_args(h, d.m, connected_account_id=spare)))

    d.m.op_platform = RaceStore(platform, other)
    with pytest.raises(OperatorCommandError):
        d.op(h, "seed_seller", seed_args(h, d.m))  # bob on his own account loses the race
    assert winner["account"] == spare
    assert _index(d) == {spare: "bob"}  # nothing left behind by the loser
    assert st.seeded_account(d.store_for("bob")) == spare
    d.m.op_platform = platform
    # bob's own account stays free: a new seller can be seeded on it
    other_seller = d.op(
        h, "seed_seller", seed_args(h, d.m, seller="cara", connected_account_id=d.m.accounts["bob"])
    )
    assert other_seller["account"] == d.m.accounts["bob"]


def test_two_sellers_racing_for_one_account_bind_exactly_one(h):
    d = Deployed(h, h.usd(0))
    shared = d.m.fake.add_connected_account("poc-i8-seller-shared", {"USD": h.usd(0)})
    platform = d.harness.store()

    def other():
        d.op(h, "seed_seller", seed_args(h, d.m, seller="alice", connected_account_id=shared))

    d.m.op_platform = RaceStore(platform, other)
    with pytest.raises(OperatorCommandError):
        d.op(h, "seed_seller", seed_args(h, d.m, connected_account_id=shared))
    assert _index(d) == {shared: "alice"}
    assert d.store_for("bob").query("") == []  # the loser wrote nothing


def test_a_crash_after_the_index_write_then_a_re_run_completes(h, monkeypatch):
    d = Deployed(h, h.usd(0))
    real = st.create_book

    def crash(kstore):
        raise RuntimeError("Lambda killed after the index write")

    monkeypatch.setattr(st, "create_book", crash)
    with pytest.raises(RuntimeError):
        d.op(h, "seed_seller", seed_args(h, d.m))
    assert _index(d) == {d.m.accounts["bob"]: "bob"}
    assert d.store_for("bob").query("") == []
    with pytest.raises(OperatorCommandError, match="already bound to seller bob"):
        d.op(
            h,
            "seed_seller",
            seed_args(h, d.m, seller="cara", connected_account_id=d.m.accounts["bob"]),
        )
    monkeypatch.setattr(st, "create_book", real)
    out = d.op(h, "seed_seller", seed_args(h, d.m))  # the same seller re-runs: completes
    assert out["account"] == d.m.accounts["bob"]
    assert st.seeded_account(d.store_for("bob")) == d.m.accounts["bob"]
    assert st.book_id(d.store_for("bob")) == out["book_id"]
