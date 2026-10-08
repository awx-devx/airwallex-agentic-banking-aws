"""Kit 1's operator commands: the only way deploy-time state reaches a deployed kit 1.

`seed_treasury_book` (once) writes the treasury book into the kit's platform-tenant store and
mints the M2 book id, through seed.seed_deployed. Args (a JSON object, checked strictly):

    {"beneficiaries": {"<obligation_id>": "<poc-i1- beneficiary id>", ...},
     "book_version": "<slug>", "replace": false}

Every beneficiary is checked through `ctx.read` (it exists and its nickname starts `poc-i1-`);
the command refuses when `ctx.read` is None. A second run is refused unless `replace` is true,
and a replace keeps the book id. It moves no money: OperatorContext has no write port.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common.operator_commands import (
    OperatorCommand,
    OperatorCommandError,
    OperatorContext,
    require_fresh_or_replace,
)

from .book import load_book
from .seed import SeedRefused, seed_deployed

_ARGS = {"beneficiaries", "book_version", "replace"}


def _seed_treasury_book(ctx: OperatorContext, args: Mapping[str, Any]) -> Mapping[str, Any]:
    extra = set(args) - _ARGS
    if extra:
        raise OperatorCommandError(f"unexpected arguments {sorted(extra)}")
    bens = args.get("beneficiaries")
    if (
        not isinstance(bens, Mapping)
        or not bens
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in bens.items())
    ):
        raise OperatorCommandError("beneficiaries must map obligation ids to beneficiary ids")
    version = args.get("book_version")
    if not isinstance(version, str) or not version:
        raise OperatorCommandError("book_version is required")
    if ctx.read is None:
        raise OperatorCommandError("seed_treasury_book checks every beneficiary; read is required")
    store = ctx.platform_store
    replacing = require_fresh_or_replace(load_book(store) is not None, args, "the treasury book")
    try:
        report = seed_deployed(
            store, dict(bens), book_version=version, read=ctx.read, replace_existing=replacing
        )
    except SeedRefused as exc:
        raise OperatorCommandError(str(exc)) from None
    return {**report, "replaced": replacing}


OPERATOR_COMMANDS: tuple[OperatorCommand, ...] = (
    OperatorCommand(
        name="seed_treasury_book",
        summary="Seed kit 1's treasury book from real poc-i1- beneficiary ids (mints the book id)",
        run=_seed_treasury_book,
        once=True,
    ),
)
