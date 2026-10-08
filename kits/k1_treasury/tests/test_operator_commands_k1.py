"""Kit 1's operator command: from EMPTY KitToolHarness stores, seed only through
run_operator_command, then drive the shipped tools end to end; the seed run twice is refused,
and a replace keeps the book id."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from awx_agent.testing import KitToolHarness
from awx_common.operator_commands import (
    OperatorCommandError,
    OperatorContext,
    run_operator_command,
)
from awx_k1_treasury import OPERATOR_COMMANDS
from awx_k1_treasury.book import load_book, load_book_id

NOW = datetime.now(UTC).replace(microsecond=0)


def _beneficiary(fake, name: str, country: str, method: str) -> str:
    from awx_common import Operation, OperationStatus, derive_request_id

    key = f"poc-i1-ben-{name}"
    rid = derive_request_id("k1", key, "create")
    bank = {"bank_account_category": "Checking", "account_routing_type1": "aba"}
    res = fake.execute(
        Operation(
            kit="k1",
            name="beneficiaries.create",
            method="POST",
            path="/api/v1/beneficiaries/create",
            body={
                "request_id": rid,
                "nickname": key,
                "transfer_methods": [method],
                "beneficiary": {
                    "type": "BANK_ACCOUNT",
                    "entity_type": "COMPANY",
                    "address": {
                        "country_code": country,
                        **({"state": "US-NY"} if country == "US" else {}),
                    },
                    "bank_details": bank if country == "US" else {},
                },
            },
            request_id=rid,
            business_key=key,
            intent_hash="b" * 64,
        )
    )
    assert res.status is OperationStatus.SUCCEEDED, res
    return str(res.provider_id)


@pytest.fixture
def world():
    from awx_common import Money
    from awx_spine.fake import FakeAirwallex

    fake = FakeAirwallex(
        balances={
            c: Money.of(v, c) for c, v in {"USD": "42000", "EUR": "2000", "GBP": "1500"}.items()
        }
    )
    h = KitToolHarness.for_kit("k1", port=fake, clock=lambda: NOW)
    ctx = OperatorContext(
        kit="k1", platform_store=h.store(), store_for=h.store, now=NOW, read=fake.read
    )
    bens = {
        "ob-shipping": _beneficiary(fake, "shipping", "US", "LOCAL"),
        "ob-office-usd": _beneficiary(fake, "office", "US", "LOCAL"),
    }
    return fake, h, ctx, bens


def seed(ctx, bens, **extra):
    return run_operator_command(
        OPERATOR_COMMANDS,
        "seed_treasury_book",
        ctx,
        {"beneficiaries": bens, "book_version": "demo-1", **extra},
    )


def test_empty_stores_seeded_only_by_the_command_then_the_tools_work(world):
    _fake, h, ctx, bens = world
    before = h.call("read_obligations", {})
    assert before["untrusted_data"]["status"] == "NOT_CONFIGURED"
    report = seed(ctx, bens)
    assert report["book_id"] == load_book_id(h.store())
    assert set(report["obligations"]) == set(bens)
    obs = h.call("read_obligations", {})["untrusted_data"]["obligations"]
    assert {o["obligation_id"] for o in obs} == set(bens)
    plan = h.call("read_treasury_plan", {})["untrusted_data"]
    assert "ob-shipping" in plan["proposable_obligation_ids"]
    out = h.call("propose_execute_plan", {"obligation_ids": ["ob-shipping"]})
    assert out["risk_tier"] == "AUTO", out
    (execution,) = h.executions
    (payout,) = execution["proposal"]["computed"]["payouts"]
    assert payout["beneficiary_id"] == bens["ob-shipping"]  # the seeded, real beneficiary


def test_the_seeded_store_runs_execute_plan_through_the_runner(world):
    from awx_common import TenantContext
    from awx_common.sandbox import make_transfer_advance
    from awx_k1_treasury.workflow import build_specs
    from awx_spine.fake import EPOCH
    from awx_spine.store import InMemoryOperationStore
    from awx_spine_aws.local_runner import run_kit_workflow

    fake, h, ctx, bens = world
    seed(ctx, bens)
    specs = build_specs(transfer_advance=make_transfer_advance("k1"))
    out = run_kit_workflow(
        specs["execute_plan"],
        {"obligation_ids": ["ob-shipping"]},
        proposed_by="requester",
        tenant=TenantContext("platform"),
        port=fake,
        store=InMemoryOperationStore(),
        decide=lambda p: [],
        now=lambda: EPOCH + timedelta(seconds=fake.clock()),
        sleep=fake.advance,
        kit_store=h.store(),
        platform_store=h.store(),
        actions=tuple(specs),
    )
    assert out.state == "SUCCEEDED", out.detail
    assert [t["status"] for t in fake.objects("transfers")] == ["PAID"]


def test_seed_twice_is_refused_and_replace_keeps_the_book_id(world):
    _fake, h, ctx, bens = world
    first = seed(ctx, bens)
    with pytest.raises(OperatorCommandError, match="already seeded"):
        seed(ctx, bens)
    again = run_operator_command(
        OPERATOR_COMMANDS,
        "seed_treasury_book",
        ctx,
        {
            "beneficiaries": {"ob-shipping": bens["ob-shipping"]},
            "book_version": "demo-2",
            "replace": True,
        },
    )
    assert again["replaced"] is True
    assert again["book_id"] == first["book_id"]
    assert load_book(h.store()).book_version == "demo-2"


@pytest.mark.parametrize(
    "args",
    [
        {"beneficiaries": {"ob-shipping": "ben-poc-k1-freight-us"}, "book_version": "v"},
        {"beneficiaries": {}, "book_version": "v"},
        {"beneficiaries": {"ob-shipping": "x"}},
        {"beneficiaries": {"ob-shipping": "x"}, "book_version": "v", "amount": "1"},
        {"beneficiaries": {"ob-shipping": "x"}, "book_version": "v", "replace": "yes"},
    ],
)
def test_bad_args_are_refused(world, args):
    _fake, h, ctx, _bens = world
    with pytest.raises(OperatorCommandError):
        run_operator_command(OPERATOR_COMMANDS, "seed_treasury_book", ctx, args)
    assert load_book(h.store()) is None


def test_read_is_required_and_every_beneficiary_is_checked(world):
    from dataclasses import replace

    _fake, h, ctx, bens = world
    with pytest.raises(OperatorCommandError, match="read is required"):
        seed(replace(ctx, read=None), bens)
    with pytest.raises(OperatorCommandError):  # an id the provider does not have
        seed(ctx, {"ob-shipping": "00000000-0000-4000-8000-000000000000"})
    assert load_book(h.store()) is None


# ---- two operators seeding at once ----


def _racer_book(bens):
    from awx_k1_treasury.seed import build_seed_book

    return build_seed_book({"ob-office-usd": bens["ob-office-usd"]}, book_version="racer")


def test_a_seed_landing_while_this_one_validates_is_not_overwritten(world):
    """The other operator's seed lands between this seed's check and its write (here, while it
    reads the beneficiaries). Before the fix the loser re-read the book and overwrote it."""
    from dataclasses import replace

    from awx_k1_treasury.book import seed_book

    fake, h, ctx, bens = world
    raced = []

    def read(req):
        if not raced:
            raced.append(True)
            seed_book(h.store(), _racer_book(bens))
        return fake.read(req)

    with pytest.raises(OperatorCommandError, match="another seed landed first"):
        seed(replace(ctx, read=read), bens)
    book = load_book(h.store())
    assert book.book_version == "racer"
    assert [o.obligation_id for o in book.obligations] == ["ob-office-usd"]


def test_a_seed_losing_the_final_put_is_refused_and_leaves_the_winner(world):
    """The race at the put itself (after the book id is ensured): a VersionConflict there is
    kit 1's specific refusal, and the winner's book and id stand."""
    from dataclasses import replace

    from awx_k1_treasury.book import BOOK_KEY, seed_book

    _fake, h, ctx, bens = world
    inner = h.store()

    class Racing:
        def __getattr__(self, name):
            return getattr(inner, name)

        def put(self, key, value, expected_version=None):
            if key == BOOK_KEY and inner.get(BOOK_KEY) is None:
                seed_book(inner, _racer_book(bens))
            return inner.put(key, value, expected_version=expected_version)

    with pytest.raises(OperatorCommandError, match="another seed landed first"):
        seed(replace(ctx, platform_store=Racing()), bens)
    assert load_book(inner).book_version == "racer"
    winner_id = load_book_id(inner)
    assert winner_id
    again = seed(ctx, bens, replace=True)
    assert again["book_id"] == winner_id  # M2: one id, whoever seeded first


# --- seed dates and the due window ------------------------------------------------------------


def _dated_world(world, now):
    fake, _h, ctx, bens = world
    h = KitToolHarness.for_kit("k1", port=fake, clock=lambda: now)
    ctx = OperatorContext(
        kit="k1", platform_store=h.store(), store_for=h.store, now=now, read=fake.read
    )
    return h, ctx, bens


def test_a_seed_without_a_start_puts_every_obligation_in_the_next_72_hours(world):
    """8 Oct 2026: a book seeded that day listed due dates of 19 and 20 Oct, and the agent
    called them due in the next 72 hours."""
    now = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
    h, ctx, bens = _dated_world(world, now)
    report = seed(ctx, bens)
    obs = h.call("read_obligations", {})["untrusted_data"]["obligations"]
    assert {o["obligation_id"]: o["due_window"] for o in obs} == dict.fromkeys(
        bens, "NEXT_72_HOURS"
    )
    for o in load_book(h.store()).obligations:
        assert now < o.due_at <= now + timedelta(hours=72)
        assert report["due"][o.obligation_id] == o.due_at.isoformat()
    # The scenario starts at the next whole hour: the shipping invoice keeps its 09:00 offset.
    assert load_book(h.store()).obligation("ob-shipping").due_at == datetime(
        2026, 10, 9, 0, tzinfo=UTC
    )


def test_scenario_start_pins_the_dates_and_keeps_their_gaps(world):
    from awx_k1_treasury.book import DEMO_BOOK

    h, ctx, bens = _dated_world(world, datetime(2026, 10, 8, 14, 30, tzinfo=UTC))
    seed(ctx, bens, scenario_start="2026-10-19")  # the guide's own dates
    book = load_book(h.store())
    for oid in bens:
        assert book.obligation(oid).due_at == DEMO_BOOK.obligation(oid).due_at
    assert book.forecasts[0].expected_at == DEMO_BOOK.forecasts[0].expected_at
    seed(ctx, bens, scenario_start="2026-11-02T00:00Z", replace=True)
    moved = load_book(h.store())
    for oid in bens:
        assert moved.obligation(oid).due_at - book.obligation(oid).due_at == timedelta(days=14)


@pytest.mark.parametrize(
    ("start", "message"),
    [
        ("19 Oct", "is not an ISO date"),
        ("2026-10-19T09:00", "needs a timezone"),
        (20261019, "must be an ISO date or date-time string"),
    ],
)
def test_a_bad_scenario_start_is_refused_and_writes_nothing(world, start, message):
    _fake, h, ctx, bens = world
    with pytest.raises(OperatorCommandError, match=message):
        seed(ctx, bens, scenario_start=start)
    assert load_book(h.store()) is None


def test_read_obligations_does_not_call_a_later_obligation_due_in_the_window(world):
    """The book's dates are 19 to 21 Oct; read on 8 Oct, none is in the next 72 hours."""
    from awx_k1_treasury.book import DEMO_BOOK

    fake, _h, _ctx, bens = world
    pinned = {"scenario_start": "2026-10-19"}
    h, ctx, _ = _dated_world(world, datetime(2026, 10, 8, 14, 30, tzinfo=UTC))
    seed(ctx, bens, **pinned)
    view = h.call("read_obligations", {})["untrusted_data"]
    assert view["due_window_hours"] == 72
    assert {o["due_window"] for o in view["obligations"]} == {"LATER"}
    noon_20 = datetime(2026, 10, 20, 12, tzinfo=UTC)
    later = KitToolHarness.for_kit("k1", port=fake, clock=lambda: noon_20)
    later.stores[("k1", "platform")] = h.store()
    windows = {
        o["obligation_id"]: o["due_window"]
        for o in later.call("read_obligations", {})["untrusted_data"]["obligations"]
    }
    assert DEMO_BOOK.obligation("ob-shipping").due_at < noon_20
    assert windows == {"ob-shipping": "OVERDUE", "ob-office-usd": "NEXT_72_HOURS"}
