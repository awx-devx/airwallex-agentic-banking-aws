"""Operator commands, the shipped entry point for state that is not model input."""

from datetime import UTC, datetime

import pytest
from awx_common.operator_commands import (
    OperatorCommand,
    OperatorCommandError,
    OperatorContext,
    check_operator_commands,
    require_fresh_or_replace,
    run_operator_command,
)
from awx_common.store import InMemoryKitStore, VersionConflict

NOW = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)


def _ctx():
    stores: dict[str, InMemoryKitStore] = {}
    return OperatorContext(
        kit="k8",
        platform_store=stores.setdefault("platform", InMemoryKitStore()),
        store_for=lambda t: stores.setdefault(t, InMemoryKitStore()),
        now=NOW,
    )


def _seed(ctx, args):
    store = ctx.store_for(args["tenant"])
    existing = store.get("profile")
    require_fresh_or_replace(existing is not None, args, "seller profile")
    store.put(
        "profile",
        {"name": args["name"]},
        expected_version=None if existing is None else existing.version,
    )
    return {"tenant": args["tenant"], "replaced": existing is not None}


SEED = OperatorCommand("seed_seller", "Seed one seller's profile.", _seed, once=True)


@pytest.mark.parametrize(
    "name", ["", "x", "Seed", "seed-seller", "1seed", "read_book", "propose_seed", "s" * 42]
)
def test_a_command_name_is_snake_case_and_never_looks_like_a_tool(name):
    with pytest.raises(ValueError, match=r"snake_case|named like a tool"):
        OperatorCommand(name, "summary", _seed)


@pytest.mark.parametrize("summary", ["", "   ", "two\nlines"])
def test_a_command_needs_a_one_line_summary(summary):
    with pytest.raises(ValueError, match="summary"):
        OperatorCommand("seed_seller", summary, _seed)


def test_run_must_be_callable_and_once_a_bool():
    with pytest.raises(ValueError, match="callable"):
        OperatorCommand("seed_seller", "summary", "not callable")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="once must be a bool"):
        OperatorCommand("seed_seller", "summary", _seed, once="yes")  # type: ignore[arg-type]


def test_check_refuses_repeated_names_and_clashes_with_tools_actions_and_functions():
    other = OperatorCommand("load_cycle", "Load one cycle.", _seed)
    assert check_operator_commands((SEED, other)) == (SEED, other)
    with pytest.raises(ValueError, match="repeat"):
        check_operator_commands((SEED, SEED))
    with pytest.raises(ValueError, match="reuse"):
        check_operator_commands((other,), actions=("load_cycle",))
    with pytest.raises(ValueError, match="reuse"):
        check_operator_commands((other,), function_names=("load_cycle",))
    with pytest.raises(ValueError, match="reuse"):
        check_operator_commands((SEED,), tool_names=("seed_seller",))
    with pytest.raises(ValueError, match="not an OperatorCommand"):
        check_operator_commands(("seed_seller",))  # type: ignore[arg-type]


def test_a_once_command_refuses_a_second_run_unless_replace_is_true():
    ctx = _ctx()
    args = {"tenant": "bob", "name": "Bob"}
    assert run_operator_command((SEED,), "seed_seller", ctx, args) == {
        "tenant": "bob",
        "replaced": False,
    }
    with pytest.raises(OperatorCommandError, match="already seeded"):
        run_operator_command((SEED,), "seed_seller", ctx, args)
    with pytest.raises(OperatorCommandError, match="true or false"):
        run_operator_command((SEED,), "seed_seller", ctx, {**args, "replace": "yes"})
    report = run_operator_command((SEED,), "seed_seller", ctx, {**args, "replace": True})
    assert report == {"tenant": "bob", "replaced": True}


def test_run_refuses_an_unknown_name_non_object_args_and_a_non_object_report():
    ctx = _ctx()
    with pytest.raises(OperatorCommandError, match="no operator command"):
        run_operator_command((SEED,), "seed_buyer", ctx, {})
    with pytest.raises(OperatorCommandError, match="JSON object"):
        run_operator_command((SEED,), "seed_seller", ctx, ["bob"])  # type: ignore[arg-type]
    bad = OperatorCommand("bad_report", "Returns a list.", lambda c, a: ["x"])  # type: ignore[arg-type,return-value]
    with pytest.raises(OperatorCommandError, match="report object"):
        run_operator_command((bad,), "bad_report", ctx, {})


def test_a_lost_write_race_is_a_refusal_not_a_raw_store_exception():
    """Two operators seeding at once: the loser gets an OperatorCommandError."""
    ctx = _ctx()
    seed = OperatorCommand("seed_seller_profile", "Seed one seller profile.", _seed, once=True)

    def racing_seed(ctx, args):
        # The other operator's seed lands between this command's check and its write.
        store = ctx.store_for(args["tenant"])
        store.get("profile")
        store.put("profile", {"name": "winner"}, expected_version=None)
        store.put("profile", {"name": args["name"]}, expected_version=None)
        return {"tenant": args["tenant"]}

    loser = OperatorCommand("seed_racing_profile", "Seed while another seed lands.", racing_seed)
    with pytest.raises(OperatorCommandError, match="another write landed first") as caught:
        run_operator_command(
            (seed, loser), "seed_racing_profile", ctx, {"tenant": "t1", "name": "x"}
        )
    assert isinstance(caught.value.__cause__, VersionConflict)
    assert ctx.store_for("t1").get("profile").value == {"name": "winner"}
