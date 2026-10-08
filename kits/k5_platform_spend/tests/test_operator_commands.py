"""Kit 5 operator commands: deploy-time state reaches the kit only through OPERATOR_COMMANDS.

Start from empty KitToolHarness stores, seed only through run_operator_command, then
drive the shipped TOOLS and SPECS end to end on the stores seeded that way. The once command
run twice is refused, and a replace keeps the book id.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from awx_agent.testing import KitToolHarness
from awx_common.operator_commands import OperatorCommandError, run_operator_command
from awx_common.store import InMemoryKitStore
from awx_k5_platform_spend import OPERATOR_COMMANDS, SPECS
from awx_k5_platform_spend.book import load_platform_setup
from awx_k5_platform_spend.ledger import CapitalLedger
from awx_k5_platform_spend.workflow import configure_platform, current_setup
from awx_spine_aws.local_runner import run_kit_workflow
from k5_support import Platform, approve, obligation, operator_context, setup_args, usd


def run(ctx, name: str, args: Mapping[str, Any]):
    return run_operator_command(OPERATOR_COMMANDS, name, ctx, args)


def platform() -> Platform:
    return Platform(obligations=(obligation("t-alpha", "ob-a1", "500"),))


def test_end_to_end_from_empty_harness_stores_seeded_only_by_operator_commands():
    configure_platform(None)
    p = platform()
    h = KitToolHarness.for_kit("k5", port=p.fake, tenants=dict(p.acct), clock=p.now)
    assert h.stores == {}  # nothing seeded by a fixture
    with pytest.raises(RuntimeError, match="no PlatformSetup"):
        current_setup()
    store = h.store("platform")
    ctx = operator_context(p, store)
    seeded = run(ctx, "seed_platform_setup", setup_args(p))
    assert seeded["tenants"] == ["t-alpha", "t-bravo", "t-charlie"]
    funded = run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    assert funded["ledger_cash"] == "1000.00 USD"
    assert funded["book_id"]

    # The shipped tools on those stores.
    token = h.issuer.token("user-requester", tenant_id="t-alpha", now=p.now().timestamp())
    proposal = h.call("propose_bridge_shortfall", {"obligation_ref": "ob-a1"}, token=token)
    assert proposal["status"] == "AWAITING_APPROVAL", proposal
    assert proposal["summary"]["computed_by_code"]["amount"]["amount"] == "300.00"

    # The shipped SPECS on the same platform store: the bridge runs to SETTLED.
    out = run_kit_workflow(
        SPECS["bridge_shortfall"],
        {"obligation_ref": "ob-a1"},
        proposed_by="user-requester",
        tenant=p.tenant("t-alpha"),
        port=p.fake,
        store=p.ops,
        decide=approve,
        now=p.now,
        sleep=p.fake.advance,
        kit_store=h.store("t-alpha"),
        platform_store=store,
        tenants=p.book.tenants,
    )
    assert out.state == "SUCCEEDED", out.detail
    assert p.movements("connected_account_transfers")[0]["status"] == "SETTLED"
    position = h.call("read_bridge_position", {}, token=token)
    assert position["untrusted_data"]["receivable"] == "300.00 USD"


def test_the_seed_is_once_and_a_replace_keeps_the_book_id():
    p = platform()
    store = InMemoryKitStore()
    ctx = operator_context(p, store)
    run(ctx, "seed_platform_setup", setup_args(p))
    book_id = run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})["book_id"]
    with pytest.raises(OperatorCommandError, match="already seeded"):
        run(ctx, "seed_platform_setup", setup_args(p))
    replaced = run(ctx, "seed_platform_setup", setup_args(p, reserve_floor="550", replace=True))
    assert replaced["replaced"] is True
    assert replaced["book_id"] == book_id
    assert replaced["book_id_kept"] is True
    assert load_platform_setup(store).config.reserve_floor == usd("550")
    assert CapitalLedger(store, usd("550")).cash == usd("1000")  # the ledger is untouched


def test_funding_is_idempotent_per_key_and_moves_no_money():
    p = platform()
    store = InMemoryKitStore()
    ctx = operator_context(p, store)
    with pytest.raises(OperatorCommandError, match="seed the platform setup first"):
        run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    run(ctx, "seed_platform_setup", setup_args(p))
    wallet = p.wallet(None)
    sent = [r for r in p.fake.requests if r[0] == "POST"]
    run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    again = run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    assert again["ledger_cash"] == "1000.00 USD"
    with pytest.raises(OperatorCommandError, match="different content"):
        run(ctx, "fund_capital", {"key": "initial", "amount": "999"})
    for bad in ({"key": "x", "amount": 10}, {"key": "x", "amount": "1.001"}, {"key": "x"}):
        with pytest.raises(OperatorCommandError):
            run(ctx, "fund_capital", bad)
    assert p.wallet(None) == wallet
    assert [r for r in p.fake.requests if r[0] == "POST"] == sent  # nothing was sent


def _with_account(p: Platform, tenant: str, account: str) -> dict[str, Any]:
    args = setup_args(p)
    for t in args["tenants"]:
        if t["tenant_id"] == tenant:
            t["connected_account_id"] = account
    return args


def test_every_connected_account_is_checked_through_read():
    p = platform()
    ctx = operator_context(p, InMemoryKitStore())
    unlabelled = p.fake.add_connected_account("someone-else", {})
    cases = {
        "acct_doesnotexist000": "does not exist",
        unlabelled: "not labelled poc-i5-",
        "acct_placeholder0001": "placeholder",
    }
    for account, why in cases.items():
        with pytest.raises(OperatorCommandError, match=why):
            run(ctx, "seed_platform_setup", _with_account(p, "t-bravo", account))
    offline = operator_context(p, InMemoryKitStore(), online=False)
    with pytest.raises(OperatorCommandError, match="checked through read"):
        run(offline, "seed_platform_setup", setup_args(p))


def test_an_inactive_account_is_refused():
    p = platform()
    store = InMemoryKitStore()
    base = operator_context(p, store)

    def read(req):
        body = dict(p.fake.read(req))
        if body.get("id") == p.acct["t-bravo"]:
            body["status"] = "SUSPENDED"
        return body

    ctx = type(base)(base.kit, base.platform_store, base.store_for, base.now, read)
    with pytest.raises(OperatorCommandError, match="not ACTIVE"):
        run(ctx, "seed_platform_setup", setup_args(p))


@pytest.mark.parametrize(
    "bad",
    [
        {"reserve_floor": 600},  # a JSON number, never an amount
        {"tenants": []},
        {"unknown": 1},
        {"max_bridge_days": 99},
        {"monthly_fees": {"t-nobody": "25"}},
    ],
)
def test_setup_args_are_strict(bad):
    p = platform()
    with pytest.raises(OperatorCommandError):
        run(operator_context(p, InMemoryKitStore()), "seed_platform_setup", setup_args(p, **bad))


def test_a_replace_cannot_orphan_capital_held_or_owed():
    p = platform()
    store = InMemoryKitStore()
    ctx = operator_context(p, store)
    run(ctx, "seed_platform_setup", setup_args(p))
    run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    CapitalLedger(store, usd("600")).place_hold("rid-1", "t-alpha", usd("100"), owner="run-a")
    dropped = setup_args(p, replace=True)
    dropped["tenants"] = [t for t in dropped["tenants"] if t["tenant_id"] != "t-alpha"]
    dropped["obligations"] = []
    dropped["monthly_fees"].pop("t-alpha")
    with pytest.raises(OperatorCommandError, match="cannot be dropped"):
        run(ctx, "seed_platform_setup", dropped)
    moved = _with_account(p, "t-alpha", p.acct["t-charlie"])
    moved["tenants"] = [t for t in moved["tenants"] if t["tenant_id"] != "t-charlie"]
    moved["replace"] = True
    moved["monthly_fees"].pop("t-charlie")
    with pytest.raises(OperatorCommandError, match="account cannot change"):
        run(ctx, "seed_platform_setup", moved)


def test_commands_are_not_reachable_as_tools():
    from awx_k5_platform_spend import TOOLS

    tool_names = {t.name for t in TOOLS}
    assert not {c.name for c in OPERATOR_COMMANDS} & (tool_names | set(SPECS))
    assert {c.name: c.once for c in OPERATOR_COMMANDS} == {
        "seed_platform_setup": True,
        "fund_capital": False,
    }


# ---- a replace never orphans money in flight ------------------------------------------


def _seeded_run_platform():
    """A platform seeded only by the operator commands, plus runner wiring on that store."""
    p = platform()
    store = InMemoryKitStore()
    ctx = operator_context(p, store)
    run(ctx, "seed_platform_setup", setup_args(p))
    run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    return p, store, ctx


def _run(p, store, action, raw, tenant):
    return run_kit_workflow(
        SPECS[action],
        raw,
        proposed_by="requester",
        tenant=p.tenant(tenant),
        port=p.fake,
        store=p.ops,
        decide=approve,
        now=p.now,
        sleep=p.fake.advance,
        kit_store=p.tenant_stores[tenant],
        platform_store=store,
        tenants=p.book.tenants,
    )


def test_a_replace_cannot_drop_an_obligation_with_a_bridge_in_flight():
    from awx_spine.fake import Fault

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    p.fake.inject(
        "connected_account_transfers.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600
    )
    out = _run(p, store, "bridge_shortfall", {"obligation_ref": "ob-a1"}, "t-alpha")
    assert out.state == "FAILED"
    assert CapitalLedger(store, usd("600")).holds()
    dropped = setup_args(p, replace=True, obligations=[])
    with pytest.raises(OperatorCommandError, match="obligation ob-a1 has a bridge held"):
        run(ctx, "seed_platform_setup", dropped)
    changed = setup_args(p, replace=True)
    changed["obligations"][0]["amount_due"] = "450"
    with pytest.raises(OperatorCommandError, match="another tenant or amount"):
        run(ctx, "seed_platform_setup", changed)
    # Nothing was rewritten, so the bridge still reconciles and posts.
    p.advance(hours=1, seconds=5)
    rec = _run(p, store, "reconcile_bridge", {"obligation_ref": "ob-a1"}, "t-alpha")
    assert rec.state == "SUCCEEDED", rec.detail
    assert CapitalLedger(store, usd("600")).receivable("t-alpha") == usd("300")


def test_a_replace_cannot_drop_a_tenant_with_a_charge_in_flight():
    from awx_spine.fake import Fault

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    p.fake.inject("charges.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=10 * 86400)
    out = _run(p, store, "collect_fee", {"period": "2026-10"}, "t-charlie")
    assert out.state == "FAILED"
    led = CapitalLedger(store, usd("600"))
    assert not led.holds()  # only the sent/ record pins charlie
    assert "t-charlie" not in led.receivables()
    dropped = setup_args(p, replace=True)
    dropped["tenants"] = [t for t in dropped["tenants"] if t["tenant_id"] != "t-charlie"]
    dropped["monthly_fees"].pop("t-charlie")
    with pytest.raises(OperatorCommandError, match="t-charlie has capital held, owed or in flight"):
        run(ctx, "seed_platform_setup", dropped)
    moved = setup_args(p, replace=True)
    other = p.fake.add_connected_account("poc-i5-t-charlie-2", {})
    for t in moved["tenants"]:
        if t["tenant_id"] == "t-charlie":
            t["connected_account_id"] = other
    with pytest.raises(OperatorCommandError, match="its account cannot change"):
        run(ctx, "seed_platform_setup", moved)


def test_a_failed_attempt_does_not_pin_its_tenant():
    from awx_spine.fake import Fault

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    p.fake.inject("charges.create", Fault.INSUFFICIENT_FUNDS)
    out = _run(p, store, "collect_fee", {"period": "2026-10"}, "t-charlie")
    assert "rejected" in out.postcondition.detail
    dropped = setup_args(p, replace=True)
    dropped["tenants"] = [t for t in dropped["tenants"] if t["tenant_id"] != "t-charlie"]
    dropped["monthly_fees"].pop("t-charlie")
    assert run(ctx, "seed_platform_setup", dropped)["replaced"] is True


@pytest.mark.parametrize("replace", [False, True])
def test_two_tenants_may_not_share_a_connected_account(replace):
    p = platform()
    store = InMemoryKitStore()
    ctx = operator_context(p, store)
    if replace:
        run(ctx, "seed_platform_setup", setup_args(p))
    shared = setup_args(p, replace=replace)
    for t in shared["tenants"]:
        if t["tenant_id"] == "t-bravo":
            t["connected_account_id"] = p.acct["t-alpha"]
    with pytest.raises(OperatorCommandError, match="one tenant only"):
        run(ctx, "seed_platform_setup", shared)


def test_dropping_a_tenant_with_only_queued_shortfalls_removes_its_queue_places():
    from awx_k5_platform_spend.book import PlatformBook

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    out = _run(p, store, "register_shortfall", {"obligation_ref": "ob-a1"}, "t-alpha")
    assert out.state == "SUCCEEDED"
    dropped = setup_args(p, replace=True, obligations=[])
    dropped["tenants"] = [t for t in dropped["tenants"] if t["tenant_id"] != "t-alpha"]
    dropped["monthly_fees"].pop("t-alpha")
    report = run(ctx, "seed_platform_setup", dropped)
    assert report["queue_places_dropped"] == 1
    book = PlatformBook(load_platform_setup(store), store)
    assert book.queue_entry(("t-alpha", "ob-a1")) is None


def test_already_recorded_is_decided_before_the_call():
    p = platform()
    ctx = operator_context(p, InMemoryKitStore())
    run(ctx, "seed_platform_setup", setup_args(p))
    assert (
        run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})["already_recorded"] is False
    )
    # Same instant (same ctx.now): still reported as a no-op re-run.
    assert (
        run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})["already_recorded"] is True
    )


# ---- tenants rows that disagree with the seed, account moves, atomic seeds ------------


class _RowResolver:
    """A tenants table whose row for one tenant points somewhere else."""

    def __init__(self, base, overrides: dict[str, str]):
        self.base, self.overrides = base, overrides

    def resolve(self, tenant_id):
        from awx_common import TenantContext

        t = self.base.resolve(tenant_id)
        if tenant_id in self.overrides:
            return TenantContext(tenant_id, self.overrides[tenant_id])
        return t


def test_money_policies_block_when_the_tenants_row_and_the_seeded_account_disagree():
    """Item 10: bravo's tenants row points at alpha's seeded account. Every policy that can
    move money for bravo BLOCKs, and nothing is sent."""
    from awx_common import TenantContext

    configure_platform(None)
    p = Platform(obligations=(obligation("t-bravo", "ob-b1", "400"),))
    store = InMemoryKitStore()
    ctx = operator_context(p, store)
    run(ctx, "seed_platform_setup", setup_args(p))
    run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    stale = TenantContext("t-bravo", p.acct["t-alpha"])
    tenants = _RowResolver(p.book.tenants, {"t-bravo": p.acct["t-alpha"]})
    cases = {
        "bridge_shortfall": {"obligation_ref": "ob-b1"},
        "register_shortfall": {"obligation_ref": "ob-b1"},
        "reconcile_bridge": {"obligation_ref": "ob-b1"},
        "collect_fee": {"period": "2026-10"},
        "collect_repayment": {},
        "issue_card": {"cardholder_ref": "ops", "program": "standard"},
        "simulate_card_spend": {
            "scenario_ref": "s-1",
            "card_id": "card-1",
            "amount": "5.00",
            "currency": "USD",
            "merchant_category_code": "5734",
        },
    }
    for action, raw in cases.items():
        out = run_kit_workflow(
            SPECS[action],
            raw,
            proposed_by="requester",
            tenant=stale,
            port=p.fake,
            store=p.ops,
            decide=approve,
            now=p.now,
            sleep=p.fake.advance,
            kit_store=p.tenant_stores["t-bravo"],
            platform_store=store,
            tenants=tenants,
        )
        assert out.state == "BLOCKED", (action, out.state, out.detail)
        assert out.proposal.reasons[0].startswith(
            "the tenants table and the seeded account disagree for tenant t-bravo"
        ), action
        assert p.acct["t-alpha"] not in " ".join(out.proposal.reasons)  # ids it owns only
    assert [r for r in p.fake.requests if r[0] == "POST"] == []


def test_the_tool_plane_reports_the_disagreement_and_starts_nothing():
    configure_platform(None)
    p = Platform(obligations=(obligation("t-bravo", "ob-b1", "400"),))
    rows = {**p.acct, "t-bravo": p.acct["t-alpha"]}
    h = KitToolHarness.for_kit("k5", port=p.fake, tenants=rows, clock=p.now)
    ctx = operator_context(p, h.store("platform"))
    run(ctx, "seed_platform_setup", setup_args(p))
    run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    token = h.issuer.token("user-requester", tenant_id="t-bravo", now=p.now().timestamp())
    out = h.call("propose_bridge_shortfall", {"obligation_ref": "ob-b1"}, token=token)
    assert out["risk_tier"] == "BLOCKED", out
    assert "disagree" in out["reasons"][0]
    assert p.acct["t-alpha"] not in str(out)
    assert all(r[0] == "GET" for r in p.fake.requests)


def test_an_account_moves_between_records_only_while_nothing_is_in_flight():
    """Item 9, the rule kit 5 follows: an account is bound to one tenant (one item, checked in
    its write). A replace may move it to a new tenant only when the old tenant has nothing held,
    owed or in flight; otherwise it is refused (test_a_replace_cannot_drop_a_tenant_with_a_
    charge_in_flight)."""
    p = platform()
    store = InMemoryKitStore()
    ctx = operator_context(p, store)
    run(ctx, "seed_platform_setup", setup_args(p))
    moved = setup_args(p, replace=True)
    for t in moved["tenants"]:
        if t["tenant_id"] == "t-charlie":
            t["tenant_id"] = "t-delta"
    moved["monthly_fees"]["t-delta"] = moved["monthly_fees"].pop("t-charlie")
    assert run(ctx, "seed_platform_setup", moved)["tenants"] == ["t-alpha", "t-bravo", "t-delta"]


def _snapshot(store):
    return [(i.key, i.version, dict(i.value)) for i in store.query("")]


def test_a_refused_seed_writes_nothing():
    """Item 11: a bad row anywhere is refused before any write."""
    p = platform()
    store = InMemoryKitStore()
    ctx = operator_context(p, store)
    run(ctx, "seed_platform_setup", setup_args(p))
    run(ctx, "fund_capital", {"key": "initial", "amount": "1000"})
    before = _snapshot(store)
    bad = setup_args(p, replace=True)
    bad["tenants"][-1]["connected_account_id"] = "acct_doesnotexist000"
    with pytest.raises(OperatorCommandError):
        run(ctx, "seed_platform_setup", bad)
    with pytest.raises(OperatorCommandError):
        run(ctx, "fund_capital", {"key": "initial", "amount": "999"})
    assert _snapshot(store) == before


def test_a_concurrent_seed_is_an_operator_refusal_and_writes_nothing():
    """Item 11: a store conflict surfaces as OperatorCommandError, never VersionConflict."""
    from awx_common.store import StoredItem

    p = platform()

    class RaceOnce(InMemoryKitStore):
        armed = True

        def put(self, key, value, *, expected_version):
            if self.armed and key == "setup":
                self.armed = False
                super().put(key, value, expected_version=None)  # another operator won the race
            return super().put(key, value, expected_version=expected_version)

    store = RaceOnce()
    ctx = operator_context(p, store)
    with pytest.raises(OperatorCommandError, match="changed while this command ran"):
        run(ctx, "seed_platform_setup", setup_args(p))
    assert isinstance(store.get("setup"), StoredItem)  # only the other operator's write
    assert store.get("setup").version == 1


# ---- fees in flight, and reads when the tenants row disagrees -------------------------


def _charlie_fee_in_flight():
    from awx_spine.fake import Fault

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    p.fake.inject("charges.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    out = _run(p, store, "collect_fee", {"period": "2026-10"}, "t-charlie")
    assert out.state == "FAILED"
    return p, store, ctx


@pytest.mark.parametrize("change", ["drop", "30"])
def test_a_replace_cannot_drop_or_change_a_fee_in_flight(change):
    p, store, ctx = _charlie_fee_in_flight()
    before = _snapshot(store)
    args = setup_args(p, replace=True)
    if change == "drop":
        args["monthly_fees"].pop("t-charlie")
    else:
        args["monthly_fees"]["t-charlie"] = change
    with pytest.raises(OperatorCommandError, match="t-charlie has a fee charge in flight"):
        run(ctx, "seed_platform_setup", args)
    assert _snapshot(store) == before
    # The refused replace left the fee, so the charge reconciles and posts exactly once.
    p.advance(hours=1, seconds=5)
    again = _run(p, store, "collect_fee", {"period": "2026-10"}, "t-charlie")
    assert again.state == "SUCCEEDED", again.detail
    assert len(p.movements("charges")) == 1
    fees = [e for e in CapitalLedger(store, usd("600")).entries() if e.tenant_id == "t-charlie"]
    assert [str(e.amount) for e in fees] == ["25.00 USD"]


def test_every_tenant_read_refuses_when_the_tenants_row_and_the_seeded_account_disagree():
    """Bravo's tenants row points at alpha's seeded account: every read refuses."""
    configure_platform(None)
    p = Platform(wallets={"t-alpha": "200", "t-bravo": "5"})
    rows = {**p.acct, "t-bravo": p.acct["t-alpha"]}
    h = KitToolHarness.for_kit("k5", port=p.fake, tenants=rows, clock=p.now)
    ctx = operator_context(p, h.store("platform"))
    run(ctx, "seed_platform_setup", setup_args(p))
    seen = len(p.fake.requests)
    token = h.issuer.token("user-requester", tenant_id="t-bravo", now=p.now().timestamp())
    calls = {
        "read_tenant_summary": {},
        "read_cards": {},
        "read_card_transactions": {"card_id": "card_1"},
        "read_bridge_position": {},
    }
    for tool, args in calls.items():
        out = h.call(tool, args, token=token)
        refused = {"refused": "the tenants table and the seeded account disagree"}
        assert out["untrusted_data"] == refused, (tool, out)
        assert p.acct["t-alpha"] not in str(out)
    assert p.fake.requests[seen:] == []  # no Airwallex call, so nothing of alpha's is read


# ---- an unseeded kit, and card programs pinned by an unresolved card issue -----------


def test_every_tool_says_not_configured_before_the_seed():
    """On empty stores every read refuses and every policy BLOCKs, with a stated
    reason; none is INTERNAL, and Airwallex gets no request."""
    from k5_support import PROPOSE_INPUTS, READ_INPUTS

    configure_platform(None)
    p = platform()
    h = KitToolHarness.for_kit("k5", port=p.fake, tenants=p.acct, clock=p.now)
    seen = len(p.fake.requests)
    token = h.issuer.token("user-requester", tenant_id="t-alpha", now=p.now().timestamp())
    for tool, args in READ_INPUTS.items():
        out = h.call(tool, args, token=token)
        assert out.get("untrusted_data") == {"refused": "kit 5 is not configured yet"}, (tool, out)
    for tool, args in PROPOSE_INPUTS.items():
        out = h.call(tool, args, token=token)
        assert "error" not in out, (tool, out)
        assert out["risk_tier"] == "BLOCKED", (tool, out)
        assert out["reasons"] == ["kit 5 is not configured yet"], (tool, out)
    assert len(READ_INPUTS) + len(PROPOSE_INPUTS) == 11
    assert p.fake.requests[seen:] == []


def test_a_replace_cannot_change_a_card_programs_terms_while_a_card_issue_is_unresolved():
    from awx_spine.fake import Fault

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    p.fake.inject("issuing.cards.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    raw = {"cardholder_ref": "ops-lead", "program": "standard"}
    out = _run(p, store, "issue_card", raw, "t-alpha")
    assert out.state != "SUCCEEDED"
    changed = setup_args(p, replace=True)
    changed["card_programs"]["standard"]["per_transaction_limit"] = "90"
    before = _snapshot(store)
    with pytest.raises(OperatorCommandError, match="card program standard has a card issue"):
        run(ctx, "seed_platform_setup", changed)
    assert _snapshot(store) == before
    p.advance(hours=1, seconds=5)
    again = _run(p, store, "issue_card", raw, "t-alpha")
    assert again.state == "SUCCEEDED", again.detail
    assert len(p.fake.objects("cards")) == 1
    assert p.tenant_stores["t-alpha"].get("card-ref/ops-lead") is not None
    # Once recorded, the program's terms may change again.
    assert run(ctx, "seed_platform_setup", changed)["replaced"] is True


# ---- a definitively refused card create unpins its program -----------------------------


def _terms_change(p):
    changed = setup_args(p, replace=True)
    changed["card_programs"]["standard"]["per_transaction_limit"] = "90"
    return changed


def test_a_refused_first_card_create_unpins_its_program():
    from awx_spine.fake import Fault

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    p.fake.inject("issuing.cards.create", Fault.REFUSE)
    out = _run(
        p, store, "issue_card", {"cardholder_ref": "ops-lead", "program": "standard"}, "t-alpha"
    )
    assert out.state == "FAILED"
    assert p.fake.objects("cards") == []
    assert store.get("card-pending/t-alpha/ops-lead").value["resolved"] == "failed"
    assert run(ctx, "seed_platform_setup", _terms_change(p))["replaced"] is True


def test_an_unknown_card_create_stays_pinned():
    from awx_spine.fake import Fault

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    p.fake.inject("issuing.cards.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=3600)
    out = _run(
        p, store, "issue_card", {"cardholder_ref": "ops-lead", "program": "standard"}, "t-alpha"
    )
    assert out.state != "SUCCEEDED"
    assert "resolved" not in store.get("card-pending/t-alpha/ops-lead").value
    with pytest.raises(OperatorCommandError, match="card program standard has a card issue"):
        run(ctx, "seed_platform_setup", _terms_change(p))


def test_a_refusal_after_an_unknown_card_create_stays_pinned():
    from awx_spine.fake import Fault

    configure_platform(None)
    p, store, ctx = _seeded_run_platform()
    raw = {"cardholder_ref": "ops-lead", "program": "standard"}
    p.fake.inject("issuing.cards.create", Fault.TIMEOUT_AFTER_SUBMIT, visible_after_s=86400)
    assert _run(p, store, "issue_card", raw, "t-alpha").state != "SUCCEEDED"
    p.fake.inject("issuing.cards.create", Fault.REFUSE)
    assert _run(p, store, "issue_card", raw, "t-alpha").state != "SUCCEEDED"
    value = store.get("card-pending/t-alpha/ops-lead").value
    assert value["unknown"] is True
    assert "resolved" not in value
    with pytest.raises(OperatorCommandError, match="card program standard has a card issue"):
        run(ctx, "seed_platform_setup", _terms_change(p))
