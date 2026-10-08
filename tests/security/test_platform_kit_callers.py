"""Kits 1 to 4 serve platform callers only, pinned in the spine.

Kits 1 to 4 move the platform's own money. For each of them, every READ and every PROPOSE tool
(through KitToolHarness) refuses with TENANT_SCOPE, before it reads its arguments, a caller
whose `tenant_id` claim is not exactly `platform`:

- a tenant with no tenants-table row at all (`k5:operator`, a mistyped id, a user created
  before its kit's rows were loaded);
- a kit 5 to 8 customer tenant that has a row;
- a caller with no tenant claim, or one carrying only `custom:tenant_id=platform` (the tenant is
  read from the `tenant_id` claim only);
- a near miss of `platform`: `Platform`, ` platform`, `platform ` and an empty claim. The match
  is exact.

An empty or non-string claim is refused as NOT_AUTHORISED, every other case as TENANT_SCOPE.
Each refusal makes zero reader calls and starts zero executions. A `platform` caller passes the
tenant check. The kits are imported from this tree, so the test covers whatever kit code the
tree holds.
"""

from __future__ import annotations

import pytest
from awx_agent.registry import load_kit_tools
from awx_agent.testing import KitToolHarness
from awx_common.kits import KIT_DIRS
from awx_common.tenancy import TenantRow
from awx_common.tools import ToolKind

PLATFORM_KITS = ("k1", "k2", "k3", "k4")
ROWS = {"cara": TenantRow("k8", "acct_k8_cara", "customer")}  # a kit 8 seller with a row
NO_CLAIM = object()  # a token with no tenant_id claim at all
CUSTOM_ONLY = object()  # a token carrying only custom:tenant_id=platform
CLAIMS = (
    "k5:operator",
    "no-such-tenant",
    "cara",
    NO_CLAIM,
    CUSTOM_ONLY,
    "Platform",
    " platform",
    "platform ",
    "",
)
IDS = {NO_CLAIM: "no-claim", CUSTOM_ONLY: "custom-only", "": "empty"}


class _Port:
    """A refused caller must never reach the provider."""

    def __init__(self) -> None:
        self.calls: list = []

    def read(self, req):
        self.calls.append(("read", req))
        raise AssertionError("a refused caller reached the provider")

    def execute(self, op):
        self.calls.append(("execute", op))
        raise AssertionError("a refused caller reached the provider")


def _tools(kit: str) -> dict[ToolKind, list[str]]:
    package = "awx_" + KIT_DIRS[kit].rsplit("/", 1)[1]
    out: dict[ToolKind, list[str]] = {ToolKind.READ: [], ToolKind.PROPOSE: []}
    for tool in load_kit_tools(kit, f"{package}.tools").tools:
        out[tool.kind].append(tool.name)
    return out


def _token(h: KitToolHarness, claim) -> str:
    # h.issuer.token signs with no tenant claim unless one is given (the harness's own
    # h.token would add the kit 1-4 default "platform").
    now = h.clock().timestamp()
    if claim is NO_CLAIM:
        return h.issuer.token(now=now)
    if claim is CUSTOM_ONLY:
        return h.issuer.token(now=now, **{"custom:tenant_id": "platform"})
    return h.issuer.token(tenant_id=claim, now=now)


def _expected(claim) -> str:
    return "NOT_AUTHORISED" if claim == "" else "TENANT_SCOPE"


@pytest.mark.parametrize("claim", CLAIMS, ids=[IDS[c] if c in IDS else repr(c) for c in CLAIMS])
@pytest.mark.parametrize("kit", PLATFORM_KITS)
def test_every_tool_refuses_a_caller_that_is_not_the_platform(kit, claim):
    tools = _tools(kit)
    assert tools[ToolKind.READ] and tools[ToolKind.PROPOSE], f"{kit} needs both tool kinds"
    port = _Port()
    h = KitToolHarness.for_kit(kit, port=port, tenants=ROWS)
    for tool in [*tools[ToolKind.READ], *tools[ToolKind.PROPOSE]]:
        # an argument no tool declares: the refusal must come before argument checks
        out = h.call(tool, {"not_an_argument": 1}, token=_token(h, claim))
        assert out.get("error", {}).get("code") == _expected(claim), (kit, tool, claim, out)
    assert h.reader.calls == []  # not one reader call
    assert h.sfn.calls == [] and h.executions == []  # not one execution
    assert port.calls == []


@pytest.mark.parametrize("kit", PLATFORM_KITS)
def test_a_platform_caller_passes_the_tenant_check(kit):
    h = KitToolHarness.for_kit(kit, port=_Port(), tenants=ROWS)
    for tool in [*_tools(kit)[ToolKind.READ], *_tools(kit)[ToolKind.PROPOSE]]:
        out = h.call(tool, {"not_an_argument": 1}, token=_token(h, "platform"))
        # past the tenant check, the undeclared argument is what refuses it
        assert out.get("error", {}).get("code") == "INVALID_INPUT", (kit, tool, out)
