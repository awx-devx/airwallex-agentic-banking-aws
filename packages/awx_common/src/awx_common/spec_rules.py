"""The one place KitWorkflowSpec's structural rules live.

KitWorkflowSpec.__post_init__ and FinalitySpec.__post_init__ call these, and so does the infra
parent when it parses a kit manifest (infra.kit_manifest), so the rules a kit's spec obeys in
its own process and the rules the CDK process re-checks on the loader's (untrusted) output
cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

ACTION = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
ALLOWLIST_PREFIX = "^/api/v1/"


def check_finality_bounds(interval_s: int, timeout_s: int) -> None:
    if interval_s <= 0 or timeout_s < interval_s:
        raise ValueError("need 0 < interval_s <= timeout_s")


def check_allowlist(patterns: Iterable[str]) -> None:
    """Every GET allowlist pattern is anchored `^/api/v1/...$` and compiles."""
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise ValueError(f"allowlist pattern {pattern!r} must be a string")
        if not pattern.startswith(ALLOWLIST_PREFIX) or not pattern.endswith("$"):
            raise ValueError(f"allowlist pattern {pattern!r} must be anchored ^/api/v1/...$")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"allowlist pattern {pattern!r} does not compile: {exc}") from None


def check_workflow_shape(
    *,
    action: str,
    step_names: Sequence[str],
    has_fan_out: bool,
    has_child: bool,
    has_child_tenant: bool,
    approval_ttl_s: float,
    read_allowlist: Iterable[str],
    platform_read_allowlist: Iterable[str],
) -> None:
    """KitWorkflowSpec's rules that do not depend on callables."""
    if not isinstance(action, str) or not ACTION.match(action):
        raise ValueError(f"action {action!r} must be snake_case")
    if has_fan_out != has_child:
        raise ValueError("fan_out and child go together")
    if has_child_tenant and not has_fan_out:
        raise ValueError("child_tenant needs a fan_out")
    if not step_names and not has_fan_out:
        raise ValueError("a spec needs steps or a fan_out")
    if len(step_names) != len(set(step_names)):
        raise ValueError("step names must be unique")
    if approval_ttl_s <= 0:
        raise ValueError("approval_ttl must be positive")
    check_allowlist([*read_allowlist, *platform_read_allowlist])
