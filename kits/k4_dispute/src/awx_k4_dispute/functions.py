"""FUNCTIONS: the Lambdas kit 4 declares as data. The generic KitStack builds them."""

from __future__ import annotations

from awx_common.functions import KitFunctionSpec, KitGrant

FUNCTIONS: tuple[KitFunctionSpec, ...] = (
    KitFunctionSpec(
        name="evidence_upload",
        handler="awx_k4_dispute.upload.handler",
        grants=(KitGrant.EVIDENCE_PUT, KitGrant.KIT_STORE),
        timeout_s=60,
        memory_mb=512,
        description=(
            "Kit 4 evidence upload: type, hash and index a file; write evidence/k4/<tenant>/"
        ),
    ),
)
