"""Operator commands: how state that is not model input reaches a deployed kit.

A deployed kit has no init hook. KitStack builds state machines, tool Lambdas and
declared FUNCTIONS, and none of them runs setup code, so the configuration, books and data feeds
that a kit's tests write through conftest need a shipped entry point that an operator runs. A kit
that needs one exports `OPERATOR_COMMANDS: tuple[OperatorCommand, ...]` from its package root,
next to TOOLS, SPECS and READS.

The agent never reaches an operator command. It is not a tool, a workflow action or a FUNCTION:
the manifest loader, KitStack and the Gateway ignore OPERATOR_COMMANDS, and a command's name may
not repeat a tool name, a workflow action or a function name. The awx-deploy CLI runs a
command with operator credentials against the deployed stores; a kit's tests run the same
command against KitToolHarness stores, both through `run_operator_command`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .ops import ReadRequest
from .store import KitStore, VersionConflict

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_TOOL_PREFIXES = ("read_", "propose_")


class OperatorCommandError(ValueError):
    """A command was refused: unknown name, bad arguments, or state it may not overwrite."""


@dataclass(frozen=True)
class OperatorContext:
    """Everything a command may touch: the kit's own stores, the clock and, online, reads.

    `platform_store` is the kit's platform-tenant partition. `store_for(tenant_id)` is the
    kit's partition for one of its own tenants; the deploy runner resolves the tenant first.
    `read` is a platform-scope Airwallex GET for checking the ids a command is given. It is
    None offline, and a command that needs it refuses rather than skipping the check silently.
    """

    kit: str
    platform_store: KitStore
    store_for: Callable[[str], KitStore]
    now: datetime
    read: Callable[[ReadRequest], Mapping[str, Any]] | None = None


Run = Callable[[OperatorContext, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class OperatorCommand:
    name: str  # snake_case, 2-41 chars, never read_* or propose_*
    summary: str  # one line for the deploy runbook
    run: Run
    once: bool = False  # a seed: a second run is refused unless args["replace"] is true

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise ValueError(f"operator command {self.name!r} must be snake_case, 2-41 chars")
        if self.name.startswith(_TOOL_PREFIXES):
            raise ValueError(f"operator command {self.name!r} must not be named like a tool")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError(f"operator command {self.name!r} needs a one-line summary")
        if "\n" in self.summary.strip():
            raise ValueError(f"operator command {self.name!r} summary must be one line")
        if not callable(self.run):
            raise ValueError(f"operator command {self.name!r} run must be callable")
        if not isinstance(self.once, bool):
            raise ValueError(f"operator command {self.name!r} once must be a bool")


def check_operator_commands(
    commands: Iterable[OperatorCommand],
    *,
    tool_names: Iterable[str] = (),
    actions: Iterable[str] = (),
    function_names: Iterable[str] = (),
) -> tuple[OperatorCommand, ...]:
    """Validate a kit's OPERATOR_COMMANDS against its own TOOLS, SPECS and FUNCTIONS."""
    cmds = tuple(commands)
    for c in cmds:
        if not isinstance(c, OperatorCommand):
            raise ValueError(f"{c!r} is not an OperatorCommand")
    names = [c.name for c in cmds]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise ValueError(f"operator command names repeat: {repeated}")
    clash = sorted(set(names) & (set(tool_names) | set(actions) | set(function_names)))
    if clash:
        raise ValueError(f"operator commands reuse a tool, action or function name: {clash}")
    return cmds


def require_fresh_or_replace(exists: bool, args: Mapping[str, Any], what: str) -> bool:
    """For a `once` command: refuse to overwrite existing state unless args["replace"] is true.

    Returns True when the command is replacing existing state. A replace must still keep any
    book id already minted (rule M2): only the seeded content changes.
    """
    replace = args.get("replace", False)
    if not isinstance(replace, bool):
        raise OperatorCommandError("replace must be true or false")
    if exists and not replace:
        raise OperatorCommandError(f"{what} is already seeded; pass replace: true to reseed")
    return exists and replace


def run_operator_command(
    commands: Iterable[OperatorCommand],
    name: str,
    ctx: OperatorContext,
    args: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Run one of a kit's commands by name: the one entry point for tests and the deploy runner."""
    if not isinstance(args, Mapping):
        raise OperatorCommandError("operator command arguments must be a JSON object")
    found = [c for c in commands if c.name == name]
    if not found:
        raise OperatorCommandError(f"kit {ctx.kit} has no operator command {name!r}")
    report = _run_refusing_conflicts(found[0], name, ctx, args)
    if not isinstance(report, Mapping):
        raise OperatorCommandError(f"operator command {name!r} must return a report object")
    return report


def _run_refusing_conflicts(
    command: OperatorCommand, name: str, ctx: OperatorContext, args: Mapping[str, Any]
) -> Any:
    """Every refusal is an OperatorCommandError, including a lost write race.

    A versioned write that loses to another writer (two operators seeding at once) stops the
    command at that write. The caller sees a refusal, never a raw store exception.
    """
    try:
        return command.run(ctx, args)
    except VersionConflict as exc:
        raise OperatorCommandError(
            f"operator command {name!r} refused: another write landed first ({exc}); "
            "read the current state before running it again"
        ) from exc
