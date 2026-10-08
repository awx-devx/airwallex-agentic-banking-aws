"""Kit-bound tenant rows, pinned in the spine: a tenants-table row belongs to one kit.

For each of kits 5 to 8, every tool (through KitToolHarness) and every SPECS action's policy
(through the local runner) refuses a caller whose row belongs to another kit, for an operator
row and a customer row of each other kit: TENANT_SCOPE (or TenantScopeViolation from the
runner), zero reader calls, zero executions. The seeded `k6:operator` row against every kit
5, 7 and 8 tool is pinned on its own.

The kits are imported from this tree, so the test covers whatever kit code the tree holds.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest
from awx_agent.registry import load_kit_tools
from awx_agent.testing import KitToolHarness
from awx_common import Origin
from awx_common.errors import TenantScopeViolation
from awx_common.kits import KIT_DIRS, KITS
from awx_common.store import InMemoryKitStore
from awx_common.tenancy import StaticTenantResolver, TenantRow
from awx_spine.store import InMemoryOperationStore
from awx_spine_aws.local_runner import run_kit_workflow

GUARDED = ("k5", "k6", "k7", "k8")
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


class _Port:
    """Records every call; a refused caller must make none."""

    def __init__(self) -> None:
        self.calls: list = []

    def read(self, req):
        self.calls.append(("read", req))
        raise AssertionError("a refused caller reached the provider")

    def execute(self, op):
        self.calls.append(("execute", op))
        raise AssertionError("a refused caller reached the provider")


def _package(kit: str) -> str:
    return "awx_" + KIT_DIRS[kit].rsplit("/", 1)[1]


def _tools(kit: str) -> list[str]:
    return sorted(t.name for t in load_kit_tools(kit, f"{_package(kit)}.tools").tools)


def _intruders(kit: str) -> list[tuple[str, TenantRow]]:
    out = []
    for other in sorted(KITS):
        if other == kit:
            continue
        out.append((f"{other}:operator", TenantRow(other, None, "operator")))
        out.append((f"{other}:customer", TenantRow(other, f"acct_{other}_c", "customer")))
    return out


CASES = [(kit, tid, row) for kit in GUARDED for tid, row in _intruders(kit)]


def _assert_refused_everywhere(kit: str, tenant_id: str, row: TenantRow) -> None:
    port = _Port()
    h = KitToolHarness.for_kit(kit, port=port, tenants={tenant_id: row})
    tools = _tools(kit)
    assert tools, f"{kit} has no tools"
    for tool in tools:
        out = h.call(tool, {}, tenant_id=tenant_id)
        assert out.get("error", {}).get("code") == "TENANT_SCOPE", (kit, tool, tenant_id, out)
    assert h.reader.calls == []  # not one reader call
    assert h.sfn.calls == [] and h.executions == []  # not one execution
    assert port.calls == []


@pytest.mark.parametrize(("kit", "tenant_id", "row"), CASES, ids=[f"{k}-{t}" for k, t, _ in CASES])
def test_every_tool_refuses_a_caller_whose_row_is_another_kits(kit, tenant_id, row):
    _assert_refused_everywhere(kit, tenant_id, row)


@pytest.mark.parametrize("kit", ["k5", "k7", "k8"])
def test_k6_operator_is_refused_by_every_tool_of_kits_5_7_and_8(kit):
    """The seeded `k6:operator` row (role operator, no account) is refused by another kit."""
    _assert_refused_everywhere(kit, "k6:operator", TenantRow("k6", None, "operator"))


@pytest.mark.parametrize("kit", GUARDED)
def test_every_specs_action_refuses_another_kits_tenant_before_policy(kit):
    specs = importlib.import_module(_package(kit)).SPECS
    assert specs
    for action, spec in sorted(specs.items()):
        for tenant_id, row in _intruders(kit):
            # resolved in its OWN kit, then handed to this kit's runner
            tenant = StaticTenantResolver({tenant_id: row}).resolve(tenant_id, kit=row.kit)
            port = _Port()
            with pytest.raises(TenantScopeViolation, match="another kit"):
                run_kit_workflow(
                    spec,
                    {},
                    proposed_by="requester-sub",
                    tenant=tenant,
                    port=port,
                    store=InMemoryOperationStore(),
                    decide=lambda p: [],
                    now=lambda: NOW,
                    sleep=lambda s: None,
                    kit_store=InMemoryKitStore(),
                    origin=Origin.AGENT,
                )
            assert port.calls == [], (kit, action, tenant_id)


@pytest.mark.parametrize("kit", GUARDED)
def test_the_runner_refuses_a_child_or_read_tenant_of_another_kit(kit):
    """A run's own resolver never hands out another kit's row (child tenants, read_tenant)."""
    resolver = StaticTenantResolver({"k6:operator": TenantRow("k6", None, "operator")}, kit=kit)
    if kit == "k6":
        assert resolver.resolve("k6:operator", kit="k6").role == "operator"
        return
    with pytest.raises(TenantScopeViolation, match="another kit"):
        resolver.resolve("k6:operator", kit=kit)
