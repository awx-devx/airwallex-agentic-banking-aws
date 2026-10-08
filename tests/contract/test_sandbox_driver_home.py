"""Kits import the sandbox transfer driver from awx_common, the package they
depend on. awx_spine.sandbox re-exports the same objects, so the two can never drift."""

import ast
import inspect

from awx_common import sandbox


def test_the_spine_reexports_the_awx_common_driver():
    from awx_spine import sandbox as spine_sandbox

    assert spine_sandbox.make_transfer_advance is sandbox.make_transfer_advance
    assert spine_sandbox.transition_operation is sandbox.transition_operation
    assert spine_sandbox.TRANSITION_OP == sandbox.TRANSITION_OP == "transfers.simulate_transition"
    assert spine_sandbox.IN_FLIGHT is sandbox.IN_FLIGHT


def test_the_awx_common_driver_imports_nothing_from_the_spine():
    tree = ast.parse(inspect.getsource(sandbox))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0].startswith("awx_spine")}
