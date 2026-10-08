"""Kit 7's deploy-time state reaches empty KitToolHarness stores only through
its operator commands, and the shipped tools and workflows then run end to end on it."""

from __future__ import annotations

import pytest
from awx_agent.testing import KitToolHarness
from awx_common.operator_commands import (
    OperatorCommandError,
    OperatorContext,
    run_operator_command,
)
from awx_k7_lending import OPERATOR_COMMANDS, SPECS
from awx_k7_lending.book import BOOK_KEY, load_book

CONFIG = {
    "currency": "USD",
    "reserve_floor": "5000.00",
    "min_disbursement": "1000.00",
    "disbursement_increment": "100.00",
    "dual_approval_above": "25000.00",
}


def loan_args(lid, tenant, acct, commitment="8000.00"):
    return {
        "loan_id": lid,
        "tenant_id": tenant,
        "connected_account_id": acct,
        "commitment": commitment,
        "fee_rate": "0.08",
        "share_rate": "0.10",
        "weekly_cap": "1500.00",
        "weekly_floor": "200.00",
        "first_week": "2026-W41",
    }


@pytest.fixture
def deployed(k7):
    """An empty harness: no conftest seeding, only the fake's accounts and the commands."""
    fake = k7.K7Fake(k7.usd(k7.SHARED_WALLET))
    accts = {lid: fake.borrower(lid) for lid in ("loan-a", "loan-c")}
    tenants = {"tenant-a": accts["loan-a"], "tenant-c": accts["loan-c"]}
    h = KitToolHarness.for_kit("k7", port=fake, tenants=tenants, clock=fake.now)
    assert h.store("platform").get(BOOK_KEY) is None

    def ctx(read=fake.read):
        return OperatorContext(
            kit="k7",
            platform_store=h.store("platform"),
            store_for=lambda t: h.store(t),
            now=fake.now(),
            read=read,
        )

    def call(tool, args, tenant):
        token = h.issuer.token(tenant_id=tenant, now=fake.now().timestamp())
        return h.call(tool, args, token=token)

    return fake, accts, h, ctx, call


def test_seed_through_commands_then_the_tools_work_end_to_end(k7, deployed):
    fake, accts, h, ctx, call = deployed
    # Before the seed every proposal is BLOCKED (no portfolio).
    out = call("propose_disburse_advance", {"loan_id": "loan-a"}, "tenant-a")
    assert out.get("risk_tier") == "BLOCKED", out
    assert "no loan portfolio is open" in out["reasons"][0]

    opened = run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    for lid, tenant in (("loan-a", "tenant-a"), ("loan-c", "tenant-c")):
        r = run_operator_command(
            OPERATOR_COMMANDS, "add_loan", ctx(), loan_args(lid, tenant, accts[lid])
        )
        assert r == {"loan_id": lid, "result": "added"}
    dep = fake.deposit(None, k7.usd("20000.00"))  # the separate sandbox step under the flock
    wallet = fake.balance("USD")
    funded = run_operator_command(
        OPERATOR_COMMANDS, "fund_capital", ctx(), {"deposit_id": dep, "amount": "20000.00"}
    )
    assert funded["result"] == "funded"
    assert fake.balance("USD") == wallet  # a command moves no money

    book = load_book(h.store("platform"))[0]
    assert book.book_id == opened["book_id"]
    assert [t.loan_id for t in book.loans()] == ["loan-a", "loan-c"]

    # The shipped tools run on the seeded state.
    pos = call("read_borrower_position", {"loan_id": "loan-a"}, "tenant-a")
    assert pos["untrusted_data"]["commitment"] == {"amount": "8000.00", "currency": "USD"}
    prop = call("propose_disburse_advance", {"loan_id": "loan-a"}, "tenant-a")
    assert prop["risk_tier"] == "SINGLE_APPROVER", prop
    assert prop["summary"]["computed_by_code"]["outcome"] == "FULL"
    assert (
        h.call(
            "read_borrower_position",
            {"loan_id": "loan-c"},
            token=h.issuer.token(tenant_id="tenant-a", now=fake.now().timestamp()),
        )["error"]["code"]
        == "TENANT_SCOPE"
    )

    # And the shipped workflow, on a store seeded the same way.
    from awx_common import TenantContext
    from awx_common.store import InMemoryKitStore
    from awx_spine.store import InMemoryOperationStore
    from awx_spine_aws.local_runner import run_kit_workflow

    tenant = TenantContext("tenant-a", accts["loan-a"])
    wo = run_kit_workflow(
        SPECS["disburse_advance"],
        {"loan_id": "loan-a"},
        proposed_by="ops",
        tenant=tenant,
        port=fake,
        store=InMemoryOperationStore(now=fake.now),
        decide=k7.approve,
        now=fake.now,
        sleep=fake.advance,
        kit_store=InMemoryKitStore(),
        platform_store=h.store("platform"),
    )
    assert wo.state == "SUCCEEDED", wo.detail
    assert load_book(h.store("platform"))[0].disbursed("loan-a") == k7.usd("8000.00")


def test_open_portfolio_twice_is_refused_and_replace_keeps_the_book_id(k7, deployed):
    _fake, accts, h, ctx, _call = deployed
    first = run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    run_operator_command(
        OPERATOR_COMMANDS, "add_loan", ctx(), loan_args("loan-a", "tenant-a", accts["loan-a"])
    )
    with pytest.raises(OperatorCommandError, match="already seeded"):
        run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    replaced = run_operator_command(
        OPERATOR_COMMANDS,
        "open_portfolio",
        ctx(),
        {**CONFIG, "reserve_floor": "6000.00", "replace": True},
    )
    assert replaced["book_id"] == first["book_id"]  # M2: minted once, kept
    assert replaced["loans"] == 1
    book = load_book(h.store("platform"))[0]
    assert book.config.reserve_floor == k7.usd("6000.00")
    with pytest.raises(OperatorCommandError, match="currency"):
        run_operator_command(
            OPERATOR_COMMANDS,
            "open_portfolio",
            ctx(),
            {
                **CONFIG,
                "currency": "AUD",
                "reserve_floor": "1.00",
                "min_disbursement": "1.00",
                "disbursement_increment": "1.00",
                "dual_approval_above": "1.00",
                "replace": True,
            },
        )


def test_add_loan_and_fund_capital_are_idempotent_feeds(k7, deployed):
    _fake, accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    args = loan_args("loan-a", "tenant-a", accts["loan-a"])
    assert run_operator_command(OPERATOR_COMMANDS, "add_loan", ctx(), args)["result"] == "added"
    assert run_operator_command(OPERATOR_COMMANDS, "add_loan", ctx(), args)["result"] == (
        "unchanged"
    )
    with pytest.raises(OperatorCommandError, match="different terms"):
        run_operator_command(
            OPERATOR_COMMANDS, "add_loan", ctx(), {**args, "commitment": "9000.00"}
        )
    dep = _fake.deposit(None, k7.usd("20000.00"))
    fund = {"deposit_id": dep, "amount": "20000.00"}
    assert run_operator_command(OPERATOR_COMMANDS, "fund_capital", ctx(), fund)["result"] == (
        "funded"
    )
    assert run_operator_command(OPERATOR_COMMANDS, "fund_capital", ctx(), fund)["result"] == (
        "unchanged"
    )
    with pytest.raises(OperatorCommandError, match="another amount"):
        run_operator_command(
            OPERATOR_COMMANDS, "fund_capital", ctx(), {"deposit_id": dep, "amount": "1.00"}
        )
    assert load_book(h.store("platform"))[0].ledger.platform_cash() == k7.usd("20000.00")


def test_add_loan_checks_the_account_through_read(k7, deployed):
    fake, accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    args = loan_args("loan-a", "tenant-a", accts["loan-a"])
    with pytest.raises(OperatorCommandError, match="needs Airwallex reads"):
        run_operator_command(OPERATOR_COMMANDS, "add_loan", ctx(read=None), args)
    stranger = fake.add_connected_account("someone-else", {})
    with pytest.raises(OperatorCommandError, match="not a poc-i7- account"):
        run_operator_command(
            OPERATOR_COMMANDS, "add_loan", ctx(), {**args, "connected_account_id": stranger}
        )
    with pytest.raises(OperatorCommandError, match="not readable"):
        run_operator_command(
            OPERATOR_COMMANDS, "add_loan", ctx(), {**args, "connected_account_id": "acct_nope"}
        )
    assert load_book(h.store("platform"))[0].loans() == ()


@pytest.mark.parametrize(
    ("name", "args", "match"),
    [
        ("add_loan", {}, "no portfolio"),
        ("fund_capital", {"deposit_id": "dep-123", "amount": "1.00"}, "no portfolio"),
        ("open_portfolio", {**CONFIG, "reserve_floor": 5000.0}, "decimal string"),
        ("open_portfolio", {**CONFIG, "extra": 1}, "unexpected"),
        ("nope", {}, "no operator command"),
    ],
)
def test_commands_refuse_bad_input(k7, deployed, name, args, match):
    _fake, _accts, _h, ctx, _call = deployed
    if name == "add_loan":
        args = loan_args("loan-a", "tenant-a", "acct_x")
    with pytest.raises(OperatorCommandError, match=match):
        run_operator_command(OPERATOR_COMMANDS, name, ctx(), args)


def test_commands_never_reach_airwallex_writes(k7, deployed):
    fake, accts, _h, ctx, _call = deployed
    before = len(fake.executed)
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    run_operator_command(
        OPERATOR_COMMANDS, "add_loan", ctx(), loan_args("loan-a", "tenant-a", accts["loan-a"])
    )
    dep = fake.deposit(None, k7.usd("5.00"))
    before = len(fake.executed)
    n = len(fake.requests)
    run_operator_command(
        OPERATOR_COMMANDS, "fund_capital", ctx(), {"deposit_id": dep, "amount": "1.00"}
    )
    assert len(fake.executed) == before
    reads = [q for q in fake.requests[n:] if q[0] == "GET"]
    assert all(q[1] == "/api/v1/financial_transactions" for q in reads)
    assert reads


# One loan per connected account, capital backed by a deposit, period mode


def test_a_second_loan_on_one_connected_account_is_refused(k7, deployed):
    """One open loan per borrower ACCOUNT, since the money keys on the account."""
    _fake, accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    run_operator_command(
        OPERATOR_COMMANDS, "add_loan", ctx(), loan_args("loan-a", "tenant-a", accts["loan-a"])
    )
    with pytest.raises(OperatorCommandError, match="connected account"):
        run_operator_command(
            OPERATOR_COMMANDS,
            "add_loan",
            ctx(),
            loan_args("loan-x", "tenant-x", accts["loan-a"]),  # another tenant, same account
        )
    assert [t.loan_id for t in load_book(h.store("platform"))[0].loans()] == ["loan-a"]


def test_fund_capital_is_backed_by_a_real_platform_deposit(k7, deployed):
    """Each capital entry is keyed on the deposit that funded it, read
    through Airwallex; one deposit backs one entry, and never for more than it brought."""
    fake, accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    dep = fake.deposit(None, k7.usd("10000.00"))
    with pytest.raises(OperatorCommandError, match="needs Airwallex reads"):
        run_operator_command(
            OPERATOR_COMMANDS, "fund_capital", ctx(read=None), {"deposit_id": dep, "amount": "1.00"}
        )
    with pytest.raises(OperatorCommandError, match="no deposit"):
        run_operator_command(
            OPERATOR_COMMANDS,
            "fund_capital",
            ctx(),
            {"deposit_id": "dep-not-there", "amount": "1.00"},
        )
    borrower_dep = fake.deposit(accts["loan-a"], k7.usd("10000.00"))  # into a borrower wallet
    with pytest.raises(OperatorCommandError, match="no deposit"):
        run_operator_command(
            OPERATOR_COMMANDS, "fund_capital", ctx(), {"deposit_id": borrower_dep, "amount": "1.00"}
        )
    with pytest.raises(OperatorCommandError, match="less than"):
        run_operator_command(
            OPERATOR_COMMANDS, "fund_capital", ctx(), {"deposit_id": dep, "amount": "10000.01"}
        )
    for bad in ("0.00", "-5.00"):
        with pytest.raises(OperatorCommandError, match="greater than zero"):
            run_operator_command(
                OPERATOR_COMMANDS, "fund_capital", ctx(), {"deposit_id": dep, "amount": bad}
            )
    ok = run_operator_command(
        OPERATOR_COMMANDS, "fund_capital", ctx(), {"deposit_id": dep, "amount": "10000.00"}
    )
    assert ok["result"] == "funded"
    entry = load_book(h.store("platform"))[0].ledger.get(f"capital:{dep}")
    assert entry is not None
    assert entry.provider_id == dep


def test_demo_mode_is_fixed_once_a_collection_is_held(k7, deployed):
    """A held collection (not just a closed week) fixes the period mode."""
    from awx_k7_lending.book import PendingCollection, update_book

    _fake, _accts, h, ctx, _call = deployed
    run_operator_command(
        OPERATOR_COMMANDS, "open_portfolio", ctx(), {**CONFIG, "demo_period_s": 300}
    )

    def hold(b):
        b._collections["loan-a"] = PendingCollection(
            "loan-a", "DEMO300S-1", k7.usd("1.00"), k7.usd("1.00"), "rid-1", "p-1", k7.T0
        )

    update_book(h.store("platform"), hold)
    with pytest.raises(OperatorCommandError, match="demo_period_s cannot change"):
        run_operator_command(
            OPERATOR_COMMANDS, "open_portfolio", ctx(), {**CONFIG, "replace": True}
        )


def test_add_loan_first_week_must_match_the_book_mode(k7, deployed):
    """ISO weeks on a weekly book, the book's own DEMO periods on a demo book."""
    _fake, accts, _h, ctx, _call = deployed
    run_operator_command(
        OPERATOR_COMMANDS, "open_portfolio", ctx(), {**CONFIG, "demo_period_s": 300}
    )
    base = loan_args("loan-a", "tenant-a", accts["loan-a"])
    for week in ("2026-W41", "DEMO600S-5"):
        with pytest.raises(OperatorCommandError, match="DEMO"):
            run_operator_command(OPERATOR_COMMANDS, "add_loan", ctx(), {**base, "first_week": week})
    ok = run_operator_command(
        OPERATOR_COMMANDS, "add_loan", ctx(), {**base, "first_week": "DEMO300S-5"}
    )
    assert ok["result"] == "added"


# Account checks, binding, the tenants-row check and write-nothing refusals


def test_add_loan_refuses_an_account_that_is_not_active(k7, deployed):
    """Item 5: the borrower's connected account must read status ACTIVE."""
    fake, _accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    pending = fake.borrower("loan-p")
    fake.simulate("account_status", account_id=pending, next_status="SUSPENDED")
    with pytest.raises(OperatorCommandError, match="SUSPENDED"):
        run_operator_command(
            OPERATOR_COMMANDS, "add_loan", ctx(), loan_args("loan-p", "tenant-p", pending)
        )
    assert load_book(h.store("platform"))[0].loans() == ()


def test_a_loan_binding_never_moves_to_another_account(k7, deployed):
    """Item 9, rule 'refuse every move': a loaded loan_id can never be re-pointed at another
    account (add_loan refuses different terms; open_portfolio replace keeps the loans)."""
    _fake, accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    args = loan_args("loan-a", "tenant-a", accts["loan-a"])
    run_operator_command(OPERATOR_COMMANDS, "add_loan", ctx(), args)
    with pytest.raises(OperatorCommandError, match="different terms"):
        run_operator_command(
            OPERATOR_COMMANDS, "add_loan", ctx(), {**args, "connected_account_id": accts["loan-c"]}
        )
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), {**CONFIG, "replace": True})
    (terms,) = load_book(h.store("platform"))[0].loans()
    assert terms.borrower_account == accts["loan-a"]


@pytest.mark.parametrize("action", ["disburse_advance", "collect_weekly_repayment"])
def test_a_stale_tenants_row_blocks_money_and_sends_nothing(k7, deployed, action):
    """Item 10: tenant-a's tenants row points at loan-c's seeded account. Every money policy
    for tenant-a BLOCKs ('the tenants table and the seeded account disagree') and nothing is
    sent to either account; no account id is in the reason."""
    fake, accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    for lid, tenant in (("loan-a", "tenant-a"), ("loan-c", "tenant-c")):
        run_operator_command(
            OPERATOR_COMMANDS, "add_loan", ctx(), loan_args(lid, tenant, accts[lid])
        )
    from awx_common import TenantContext
    from awx_common.store import InMemoryKitStore
    from awx_spine.store import InMemoryOperationStore
    from awx_spine_aws.local_runner import run_kit_workflow

    stale = TenantContext("tenant-a", accts["loan-c"])  # the wrong row
    raw = {"loan_id": "loan-a"}
    if action == "collect_weekly_repayment":
        raw = {"loan_id": "loan-a", "week": "2026-W42"}
        fake.move_to(k7.AFTER_W42)
    sent = len(fake.executed)
    wo = run_kit_workflow(
        SPECS[action],
        raw,
        proposed_by="ops",
        tenant=stale,
        port=fake,
        store=InMemoryOperationStore(now=fake.now),
        decide=k7.approve,
        now=fake.now,
        sleep=fake.advance,
        kit_store=InMemoryKitStore(),
        platform_store=h.store("platform"),
    )
    assert wo.state == "BLOCKED", (wo.state, wo.detail)
    reason = " ".join(wo.proposal.reasons)
    assert reason == "the tenants table and the seeded account disagree"
    assert accts["loan-c"] not in repr(wo)
    assert accts["loan-a"] not in reason
    assert len(fake.executed) == sent
    # The same through the agent: the borrower's tool call is BLOCKED, nothing started.
    h.dispatcher.env.resolver.tenants["tenant-a"] = accts["loan-c"]
    token = h.issuer.token(tenant_id="tenant-a", now=fake.now().timestamp())
    out = h.call(f"propose_{action}", raw, token=token)
    assert out["risk_tier"] == "BLOCKED", out
    assert out["reasons"] == ["the tenants table and the seeded account disagree"]
    assert accts["loan-c"] not in repr(out)
    assert "acct_" not in repr(out)


def test_a_refused_command_writes_nothing(k7, deployed):
    """Item 11: every refusal leaves the store exactly as it was, and a concurrent write is an
    OperatorCommandError, never a VersionConflict."""
    fake, accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    run_operator_command(
        OPERATOR_COMMANDS, "add_loan", ctx(), loan_args("loan-a", "tenant-a", accts["loan-a"])
    )
    store = h.store("platform")
    before = store.get(BOOK_KEY)
    dep = fake.deposit(None, k7.usd("100.00"))
    refusals = [
        ("add_loan", {**loan_args("loan-a", "tenant-a", accts["loan-a"]), "commitment": "1.00"}),
        ("add_loan", loan_args("loan-z", "tenant-z", accts["loan-a"])),  # shared account
        ("fund_capital", {"deposit_id": dep, "amount": "100.01"}),
        ("fund_capital", {"deposit_id": dep, "amount": "0.00"}),
        ("open_portfolio", CONFIG),
        (
            "open_portfolio",
            {
                **CONFIG,
                "currency": "AUD",
                "reserve_floor": "1.00",
                "min_disbursement": "1.00",
                "disbursement_increment": "1.00",
                "dual_approval_above": "1.00",
                "replace": True,
            },
        ),
    ]
    for name, args in refusals:
        with pytest.raises(OperatorCommandError):
            run_operator_command(OPERATOR_COMMANDS, name, ctx(), args)
        after = store.get(BOOK_KEY)
        assert (after.version, after.value) == (before.version, before.value), name

    class Racing:
        """A store whose write loses a race: the book moved between read and put."""

        def get(self, key):
            return store.get(key)

        def put(self, key, value, *, expected_version):
            from awx_common.store import VersionConflict

            raise VersionConflict(f"{key} moved")

        def query(self, prefix):
            return store.query(prefix)

    racing = OperatorContext(
        kit="k7",
        platform_store=Racing(),
        store_for=lambda t: Racing(),
        now=fake.now(),
        read=fake.read,
    )
    with pytest.raises(OperatorCommandError, match="concurrently"):
        run_operator_command(
            OPERATOR_COMMANDS, "add_loan", racing, loan_args("loan-c", "tenant-c", accts["loan-c"])
        )
    with pytest.raises(OperatorCommandError, match="concurrently"):
        run_operator_command(
            OPERATOR_COMMANDS, "fund_capital", racing, {"deposit_id": dep, "amount": "1.00"}
        )


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_borrower_position", {"loan_id": "loan-a"}),
        ("read_weekly_collection_preview", {"loan_id": "loan-a", "week": "2026-W42"}),
        ("read_borrower_position", {"loan_id": "loan-c"}),
        ("read_weekly_collection_preview", {"loan_id": "loan-c", "week": "2026-W42"}),
    ],
)
def test_a_stale_tenants_row_blocks_every_borrower_read(k7, deployed, tool, args):
    """The tenants-row check for reads: tenant-a's row points at loan-c's seeded account.
    Every borrower read answers only the fixed reason: no figure, no other tenant's id, and no
    Airwallex request for the other account."""
    fake, accts, h, ctx, _call = deployed
    run_operator_command(OPERATOR_COMMANDS, "open_portfolio", ctx(), CONFIG)
    for lid, tenant in (("loan-a", "tenant-a"), ("loan-c", "tenant-c")):
        run_operator_command(
            OPERATOR_COMMANDS, "add_loan", ctx(), loan_args(lid, tenant, accts[lid])
        )
    fake.deposit(accts["loan-c"], k7.usd("777.00"))  # a figure that must not leak
    fake.move_to(k7.AFTER_W42)
    h.dispatcher.env.resolver.tenants["tenant-a"] = accts["loan-c"]
    n = len(fake.requests)
    token = h.issuer.token(tenant_id="tenant-a", now=fake.now().timestamp())
    out = h.call(tool, args, token=token)
    assert out == {
        "tool": tool,
        "untrusted_data": {"blocked": "the tenants table and the seeded account disagree"},
    }, out
    assert "777" not in repr(out)
    assert accts["loan-c"] not in repr(out)
    assert "acct_" not in repr(out)
    assert "tenant-c" not in repr(out)
    assert "loan-c" not in repr(out)
    assert not [q for q in fake.requests[n:] if q[2] == accts["loan-c"]]


def test_every_tool_gives_a_stated_answer_before_the_seed(k7, deployed):
    """On empty stores every kit 7 tool answers in words. A read refuses with
    "no loan portfolio is open", a proposal BLOCKs with it, the platform-scope tools refuse a
    borrower (TENANT_SCOPE); none is INTERNAL, and the fake receives no request."""
    from awx_k7_lending import TOOLS

    fake, _accts, h, _ctx, call = deployed
    args = {
        "read_borrower_position": {"loan_id": "loan-a"},
        "read_weekly_collection_preview": {"loan_id": "loan-a", "week": "2026-W42"},
        "read_portfolio_position": {},
        "propose_collect_weekly_repayment": {"loan_id": "loan-a", "week": "2026-W42"},
        "propose_disburse_advance": {"loan_id": "loan-a"},
        "propose_reconcile_portfolio": {},
    }
    assert set(args) == {t.name for t in TOOLS}
    n = len(fake.requests)
    for tool, a in args.items():
        out = call(tool, a, "tenant-a")
        assert out.get("error", {}).get("code") != "INTERNAL", (tool, out)
        if tool in ("read_portfolio_position", "propose_reconcile_portfolio"):
            assert out["error"]["code"] == "TENANT_SCOPE", (tool, out)
        elif tool.startswith("read_"):
            assert out["untrusted_data"] == {"blocked": "no loan portfolio is open"}, out
        else:
            assert out["risk_tier"] == "BLOCKED", (tool, out)
            assert out["reasons"] == ["no loan portfolio is open"], out
    assert fake.requests[n:] == []
    # A BLOCKED proposal still records its execution (the dispatcher's design); it ends at
    # the state machine's BLOCKED choice, and nothing is sent.
    assert all('"risk_tier":"BLOCKED"' in c["input"] for c in h.sfn.calls)


def test_the_platform_read_states_not_configured_too(k7, deployed):
    """The platform-scope read, called directly as an operator would, also answers in words."""
    from awx_k7_lending import READS

    fake, _accts, h, _ctx, _call = deployed
    from awx_common import TenantContext
    from awx_common.workflow import PolicyContext

    ctx = PolicyContext(
        tenant=TenantContext("platform"),
        now=fake.now(),
        read=fake.read,
        platform_store=h.store("platform"),
    )
    n = len(fake.requests)
    assert READS["read_portfolio_position"]({}, ctx) == {"blocked": "no loan portfolio is open"}
    assert fake.requests[n:] == []
