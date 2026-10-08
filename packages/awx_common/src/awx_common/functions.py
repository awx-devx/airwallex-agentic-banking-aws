"""Kit functions: Lambdas a kit declares as data; the generic KitStack builds them.

A kit writes no CDK. When it needs a function of its own (kit 4's evidence upload handler), it
exports `awx_k<N>_<slug>.functions.FUNCTIONS: tuple[KitFunctionSpec, ...]`. KitStack builds
each one in the kit stack with its own role (trusted by lambda.amazonaws.com only), its own log
group, the grants it declares from the closed KitGrant menu and nothing else. There is no
trigger, URL, alias, permission or resource policy: an operator invokes it directly by the name
KitStack emits as a (non-exported) stack output. The environment is set by KitStack only.

Adding a grant means changing KitStack, whose test pins the exact IAM statements for each grant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

MAX_TIMEOUT_S = 300
MAX_MEMORY_MB = 1024
MIN_MEMORY_MB = 128
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_HANDLER = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$")


class KitGrant(StrEnum):
    """What a kit function may be granted. Each maps to exact IAM statements in KitStack."""

    EVIDENCE_PUT = "EVIDENCE_PUT"  # s3:PutObject/AbortMultipartUpload on evidence/<kit>/*
    KIT_STORE = "KIT_STORE"  # the step roles' ledger actions, LeadingKeys <kit>#* only
    OWN_READER = "OWN_READER"  # lambda:InvokeFunction on this kit's own reader only


@dataclass(frozen=True)
class KitFunctionSpec:
    name: str  # snake_case; the Lambda's construct id and output name derive from it
    handler: str  # "awx_k<N>_<slug>.module.function", inside the kit's own package
    grants: tuple[KitGrant, ...] = ()
    timeout_s: int = 30
    memory_mb: int = 256
    description: str = ""

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"function name {self.name!r} must be snake_case, 2-41 chars")
        if not _HANDLER.match(self.handler):
            raise ValueError(f"handler {self.handler!r} must be a dotted module.function path")
        grants = tuple(self.grants)
        for g in grants:
            if not isinstance(g, KitGrant):
                raise ValueError(f"unknown grant {g!r}; grants come from KitGrant only")
        if len(set(grants)) != len(grants):
            raise ValueError("a grant is listed twice")
        object.__setattr__(self, "grants", grants)
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, int):
            raise ValueError("timeout_s must be an int")
        if not 1 <= self.timeout_s <= MAX_TIMEOUT_S:
            raise ValueError(f"timeout_s must be 1..{MAX_TIMEOUT_S}")
        if isinstance(self.memory_mb, bool) or not isinstance(self.memory_mb, int):
            raise ValueError("memory_mb must be an int")
        if not MIN_MEMORY_MB <= self.memory_mb <= MAX_MEMORY_MB:
            raise ValueError(f"memory_mb must be {MIN_MEMORY_MB}..{MAX_MEMORY_MB}")

    def check_package(self, package: str) -> None:
        """The handler must live inside the kit's own package (not the package itself)."""
        module = self.handler.rsplit(".", 1)[0]
        if not module.startswith(f"{package}."):
            raise ValueError(f"handler {self.handler!r} is outside the kit package {package}")
