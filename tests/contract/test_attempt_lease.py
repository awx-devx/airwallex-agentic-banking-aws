"""The executor's attempt lease outlasts any live send.

A second caller must never take a send that is still running in the adapter Lambda for a crash,
so the default lease is longer than the deployed adapter's timeout. Runs in `make check` (no
sandbox marker, no network).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_default_attempt_lease_outlasts_the_adapter_lambda_timeout():
    from awx_spine.executor import DEFAULT_ATTEMPT_LEASE_S

    from infra.stacks.spine_stack import ADAPTER_TIMEOUT_S

    assert DEFAULT_ATTEMPT_LEASE_S > ADAPTER_TIMEOUT_S
    assert DEFAULT_ATTEMPT_LEASE_S >= ADAPTER_TIMEOUT_S + 30  # a margin for store writes
