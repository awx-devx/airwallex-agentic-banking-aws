"""Kit 8 workflow specs: settle a seller's cycle, recalculate one seller's reserve on carrier
evidence, and recover a refund shortfall.

Each spec runs in ONE seller tenant and reads and writes only that seller's KitStore
(ctx.store). Tool inputs are ids; code computes every amount. See settle.py, recalculate.py and
recover.py for each action's resolution rules.
"""

from __future__ import annotations

from awx_common.workflow import KitWorkflowSpec

from .recalculate import make_recalculate
from .reconcile import make_reconcile
from .recover import make_recover
from .reserve import DEFAULT_POLICY, ReservePolicy
from .resolve import make_resolve
from .settle import make_settle


def make_specs(policy: ReservePolicy = DEFAULT_POLICY) -> dict[str, KitWorkflowSpec]:
    specs = (
        make_settle(policy),
        make_recalculate(policy),
        make_recover(policy),
        make_reconcile(),
        make_resolve(),
    )
    return {s.action: s for s in specs}


SPECS: dict[str, KitWorkflowSpec] = make_specs()
