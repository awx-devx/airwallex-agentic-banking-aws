"""Kit 7's tool surface through the real ToolDispatcher (awx_agent.testing.KitToolHarness):
approval bound to the proposal hash, tenant isolation, no model-chosen amount, account or
request_id, and a re-proposal after its execution closed."""

from __future__ import annotations

import pytest
from awx_agent.testing import ADAPTER_FN, KitToolHarness
from awx_k7_lending import TOOLS
from awx_k7_lending.book import BOOK_KEY
from awx_spine_aws import codec

ALL_TOOLS = sorted(t.name for t in TOOLS)
SERVER_OWNED = ("amount", "connected_account_id", "request_id", "account_id", "tenant_id")


@pytest.fixture
def h(k7, portfolio_world):
    w = portfolio_world
    tenants = {t.tenant_id: t.connected_account_id for t in w.borrowers.values()}
    harness = KitToolHarness.for_kit("k7", port=w.fake, tenants=tenants, clock=w.fake.now)
    item = w.store.get(BOOK_KEY)
    harness.store("platform").put(BOOK_KEY, item.value, expected_version=None)
    real_call = harness.call

    def call(tool, arguments, *, tenant_id=None, **kw):
        # Tokens are issued at the fake clock the dispatcher verifies against.
        token = harness.issuer.token(tenant_id=tenant_id, now=w.fake.now().timestamp())
        return real_call(tool, arguments, token=token, **kw)

    harness.call = call
    return harness


@pytest.fixture
def portfolio_world(k7, world):
    """A disbursed (W41), W42 revenue staged, clock after W42: every tool has work to do."""
    from awx_k7_lending import SPECS

    out = world.run(
        SPECS["disburse_advance"], {"loan_id": "loan-a"}, tenant=world.borrowers["loan-a"]
    )
    assert out.post.ok, out.post.detail
    world.fake.move_to(k7.IN_W42)
    world.fake.deposit(world.borrowers["loan-a"].connected_account_id, k7.usd("10000.00"))
    world.fake.move_to(k7.AFTER_W42)
    return world


def _args(tool, k7):
    if tool in ("read_weekly_collection_preview", "propose_collect_weekly_repayment"):
        return {"loan_id": "loan-a", "week": k7.W42}
    if tool in ("read_borrower_position", "propose_disburse_advance"):
        return {"loan_id": "loan-a"}
    return {}


def test_every_tool_is_reachable_and_never_invokes_the_adapter(k7, h):
    for tool in ALL_TOOLS:
        out = h.call(tool, _args(tool, k7), tenant_id="tenant-a")
        if tool in ("read_portfolio_position", "propose_reconcile_portfolio"):
            # Platform-scope tools: kits 5-8 always run as the caller's tenant, so a borrower
            # caller is refused and sees nothing (FINDING: reachable by schedule/operator only).
            assert out["error"]["code"] == "TENANT_SCOPE", out
        else:
            assert "error" not in out, (tool, out)
    assert ADAPTER_FN not in {c["FunctionName"] for c in h.reader.calls}


def test_platform_caller_is_refused_on_a_connected_account_kit(k7, h):
    for tool in ALL_TOOLS:
        out = h.call(tool, _args(tool, k7))  # no tenant claim
        assert out["error"]["code"] == "NOT_AUTHORISED", (tool, out)
    assert h.sfn.calls == []


def test_reads_carry_no_account_id(k7, h):
    for tool in ("read_borrower_position", "read_weekly_collection_preview"):
        out = h.call(tool, _args(tool, k7), tenant_id="tenant-a")
        assert "acct_" not in repr(out["untrusted_data"]), tool


@pytest.mark.parametrize("tool", ["propose_disburse_advance", "propose_collect_weekly_repayment"])
def test_approval_binds_to_the_proposal_hash(k7, h, tool):
    if tool == "propose_disburse_advance":
        args, tenant = {"loan_id": "loan-c"}, "tenant-c"
    else:
        args, tenant = _args(tool, k7), "tenant-a"
    out = h.call(tool, args, tenant_id=tenant)
    assert "error" not in out, out
    assert out["risk_tier"] in ("SINGLE_APPROVER", "DUAL_APPROVER")  # agent floor (model-started)
    assert out["origin"] == "AGENT"
    (execution,) = h.executions
    proposal = codec.proposal_from(execution["proposal"])
    proposal.verify_hash()
    assert proposal.intent_hash == out["intent_hash"]
    assert proposal.proposal_id == out["proposal_id"]
    assert h.sfn.calls[0]["name"] == out["proposal_id"]  # the execution name


@pytest.mark.parametrize("field", SERVER_OWNED)
@pytest.mark.parametrize("tool", ["propose_disburse_advance", "propose_collect_weekly_repayment"])
def test_model_values_never_choose_an_amount_account_or_request_id(k7, h, tool, field):
    args = {**_args(tool, k7), field: "acct_attacker" if "account" in field else "999999"}
    out = h.call(tool, args, tenant_id="tenant-a")
    assert out["error"]["code"] == "INVALID_INPUT", out
    assert h.sfn.calls == []


def test_computed_amount_and_request_id_come_from_code(k7, h):
    out = h.call(
        "propose_collect_weekly_repayment",
        _args("propose_collect_weekly_repayment", k7),
        tenant_id="tenant-a",
    )
    computed = out["summary"]["computed_by_code"]
    assert computed["collect"] == {"amount": "1000.00", "currency": "USD"}
    from awx_k7_lending.book import load_book
    from awx_k7_lending.provider import collection_request_id

    book, _ = load_book(h.store("platform"))
    assert computed["request_id"] == collection_request_id(book.book_id, "loan-a", k7.W42, 1)


def test_tenant_isolation_on_reads_and_proposals(k7, h):
    assert (
        h.call("read_borrower_position", {"loan_id": "loan-b"}, tenant_id="tenant-a")["error"][
            "code"
        ]
        == "TENANT_SCOPE"
    )
    assert (
        h.call("propose_disburse_advance", {"loan_id": "loan-c"}, tenant_id="tenant-a")["error"][
            "code"
        ]
        == "TENANT_SCOPE"
    )
    assert (
        h.call("read_borrower_position", {"loan_id": "loan-a"}, tenant_id="tenant-unknown")[
            "error"
        ]["code"]
        == "TENANT_SCOPE"
    )
    assert h.sfn.calls == []


def test_a_borrower_finds_its_own_loan_without_naming_it(k7, h):
    """On 8 Oct 2026 the deployed agent asked a borrower for its loan id. A read with no
    loan_id lists the caller's own loans, and with one loan returns its position."""
    out = h.call("read_borrower_position", {}, tenant_id="tenant-a")
    assert "error" not in out, out
    data = out["untrusted_data"]
    assert data["your_loan_ids"] == ["loan-a"]
    assert data["loan_id"] == "loan-a"
    for other in ("loan-b", "loan-c"):
        assert other not in repr(out), other
    named = h.call("read_borrower_position", {"loan_id": "loan-a"}, tenant_id="tenant-a")
    assert named["untrusted_data"]["your_loan_ids"] == ["loan-a"]
    (spec,) = [t for t in TOOLS if t.name == "propose_disburse_advance"]
    assert "call read_borrower_position without a loan_id" in spec.description


@pytest.mark.parametrize("letter", ["a", "b", "c"])
def test_each_borrower_sees_only_its_own_loan_id(k7, h, letter):
    out = h.call("read_borrower_position", {}, tenant_id=f"tenant-{letter}")
    assert "error" not in out, out
    assert out["untrusted_data"]["your_loan_ids"] == [f"loan-{letter}"]
    for other in {"a", "b", "c"} - {letter}:
        assert f"loan-{other}" not in repr(out), other


def test_a_re_proposal_after_the_execution_closed_reports_existing(k7, h):
    args = {"loan_id": "loan-c"}
    first = h.call("propose_disburse_advance", args, tenant_id="tenant-c")
    again = h.call("propose_disburse_advance", args, tenant_id="tenant-c")
    assert again["proposal_id"] == first["proposal_id"]  # same window, same execution
    (arn,) = h.sfn.executions
    h.sfn.status[arn] = "SUCCEEDED"
    closed = h.call("propose_disburse_advance", args, tenant_id="tenant-c")
    assert closed["status"] == "EXISTING_PROPOSAL_EXECUTION_SUCCEEDED", closed
    assert closed["existing_proposal"] is True
    assert len(h.sfn.executions) == 1


# A borrower never sees platform-scope figures


def _platform_figures_world(k7, capital):
    """Distinct platform figures: floor 4321.00, wallet 50000.00, loan-a's 8000.00 tranche held
    in flight on a lost send, and the given ledger capital."""
    from awx_k7_lending import SPECS

    w = k7.make_portfolio(capital=capital, reserve_floor=k7.usd("4321.00"))
    w.fake.lose_next("connected_account_transfers.create")
    out = w.run(SPECS["disburse_advance"], {"loan_id": "loan-a"}, tenant=w.borrowers["loan-a"])
    assert not out.post.ok  # held: 8000.00 in flight
    tenants = {t.tenant_id: t.connected_account_id for t in w.borrowers.values()}
    h = KitToolHarness.for_kit("k7", port=w.fake, tenants=tenants, clock=w.fake.now)
    item = w.store.get(BOOK_KEY)
    h.store("platform").put(BOOK_KEY, item.value, expected_version=None)

    def call(tool, args):
        token = h.issuer.token(tenant_id="tenant-c", now=w.fake.now().timestamp())
        return h.call(tool, args, token=token)

    return w, h, call


def _says_amount_not_chosen(reasons):
    return any("borrower cannot choose the amount" in r for r in reasons)


@pytest.mark.parametrize(
    ("path", "capital"),
    [("full", "33333.00"), ("partial", "17777.00"), ("delayed", "13000.00")],
)
def test_disbursement_result_says_the_borrower_cannot_choose_the_amount(k7, path, capital):
    """On 8 Oct 2026 a borrower asked for 2,000 USD, got the 8,000 USD tranche, and the agent
    invented a reason. The tool and every result say the amount is not the borrower's."""
    (spec,) = [t for t in TOOLS if t.name == "propose_disburse_advance"]
    assert "cannot choose the amount" in spec.description
    _w, _h, call = _platform_figures_world(k7, capital)
    out = call("propose_disburse_advance", {"loan_id": "loan-c"})
    assert "error" not in out, out
    assert _says_amount_not_chosen(out["reasons"]), (path, out["reasons"])


def test_resent_tranche_result_says_the_borrower_cannot_choose_the_amount(k7):
    w, h, _call = _platform_figures_world(k7, "33333.00")
    token = h.issuer.token(tenant_id="tenant-a", now=w.fake.now().timestamp())
    out = h.call("propose_disburse_advance", {"loan_id": "loan-a"}, token=token)
    assert "error" not in out, out
    assert out["summary"]["computed_by_code"]["resend"] is True, out
    assert _says_amount_not_chosen(out["reasons"]), out["reasons"]


@pytest.mark.parametrize(
    ("path", "capital", "usable"),
    [
        ("full", "33333.00", "25333.00"),
        ("partial", "17777.00", "9777.00"),
        ("delayed", "13000.00", "5000.00"),
    ],
)
def test_no_platform_figure_reaches_a_borrower(k7, path, capital, usable):
    w, _h, call = _platform_figures_world(k7, capital)
    platform = {
        "ledger cash": capital,
        "platform wallet": k7.SHARED_WALLET,
        "in flight": "8000.00",
        "usable": usable,
        "reserve floor": "4321.00",
    }
    outputs = [
        call("propose_disburse_advance", {"loan_id": "loan-c"}),
        call("read_borrower_position", {"loan_id": "loan-c"}),
    ]
    first = outputs[0]
    assert "error" not in first, first
    outcome = first["summary"]["computed_by_code"]["outcome"]
    assert outcome == {"full": "FULL", "partial": "PARTIAL", "delayed": "DELAY"}[path]
    text = repr(outputs)
    for label, figure in platform.items():
        assert figure not in text, (path, label, figure)
    for word in ("ledger cash", "platform wallet", "in flight", "usable", "floor 4321"):
        assert word not in text, (path, word)
    # Operators still see it: the sizing is recorded at build and in read_portfolio_position.
    from awx_k7_lending import READS
    from awx_k7_lending.book import load_book

    ctx = w.ctx(__import__("awx_k7_lending").SPECS["reconcile_portfolio"], k7.PLATFORM)
    pos = READS["read_portfolio_position"]({}, ctx)
    assert pos["reserve_floor"] == k7.usd("4321.00")
    assert pos["in_flight"] == k7.usd("8000.00")
    assert load_book(w.store)[0].sizings()  # loan-a's tranche, sized with the platform figures
