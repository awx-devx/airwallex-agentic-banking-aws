"""A kit's OPERATOR_COMMANDS never share a name with its TOOLS, SPECS or FUNCTIONS,
so nothing the agent can call is also an operator command. A kit with nothing to seed exports
none (an empty tuple, or no OPERATOR_COMMANDS at all)."""

import importlib

import pytest
from awx_common.kits import KIT_DIRS
from awx_common.operator_commands import OperatorCommand, check_operator_commands


def _package(kit: str) -> str:
    return "awx_" + KIT_DIRS[kit].rsplit("/", 1)[-1]


def _function_names(package: str) -> list[str]:
    try:
        module = importlib.import_module(f"{package}.functions")
    except ModuleNotFoundError as exc:
        if exc.name != f"{package}.functions":
            raise
        return []
    return [f.name for f in getattr(module, "FUNCTIONS", ())]


@pytest.mark.parametrize("kit", sorted(KIT_DIRS))
def test_operator_commands_are_well_formed_and_never_reachable_as_a_tool(kit):
    package = _package(kit)
    module = importlib.import_module(package)
    commands = getattr(module, "OPERATOR_COMMANDS", ())
    assert isinstance(commands, tuple), f"{package}.OPERATOR_COMMANDS must be a tuple"
    assert all(isinstance(c, OperatorCommand) for c in commands)
    check_operator_commands(
        commands,
        tool_names=[t.name for t in module.TOOLS],
        actions=list(module.SPECS),  # SPECS maps each workflow action to its spec
        function_names=_function_names(package),
    )
