"""A crash inside the attempt lease, end to end through local_runner.

A real adapter Lambda timeout kills the process before Python can record anything, so the
operation record stays PENDING with a fresh attempt_started_at. The engine's CRASHED resend
waits out the attempt lease (LEASE_RESEND_WAIT_S), in the run's own clock, so the executor then
treats the attempt as crashed and reads it back: a landed send is recorded once and never sent
again. A resend that fell inside the lease would stop the run at OUTCOME_UNKNOWN for a person.
No network, no AWS.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOY = ROOT / "packages" / "awx_spine_aws" / "tests"
if str(TOY) not in sys.path:
    sys.path.insert(0, str(TOY))

import toy  # noqa: E402
from awx_common import Origin  # noqa: E402
from awx_common.ops import OperationStatus  # noqa: E402
from awx_common.tenancy import TenantContext  # noqa: E402
from awx_spine.executor import IN_PROGRESS_CODE  # noqa: E402
from awx_spine.store import InMemoryOperationStore  # noqa: E402
from awx_spine_aws import engine  # noqa: E402
from awx_spine_aws.local_runner import run_kit_workflow  # noqa: E402


class LambdaKilledStore(InMemoryOperationStore):
    """Drops the executor's own crash record, as a killed Lambda never writes it."""

    def complete(self, request_id, result):
        if result.error_message and "the send raised" in result.error_message:
            return self.get(request_id)
        return super().complete(request_id, result)


def test_a_killed_adapter_is_read_back_after_the_lease_and_recorded_once():
    port = toy.ToyPort(crash_times=Counter({"transfers.create": 1}))  # landed, then killed
    clock = toy.Clock()
    store = LambdaKilledStore(now=clock.now)
    outcome = run_kit_workflow(
        toy.toy_spec(),
        toy.RAW,
        proposed_by="requester-sub",
        tenant=TenantContext("platform"),
        origin=Origin.SCHEDULE,
        port=port,
        store=store,
        decide=toy.no_decision,
        now=clock.now,
        sleep=clock.sleep,
    )
    transfers = [s for s in port.sends if s.name == "transfers.create"]
    fee_sends = [s for s in transfers if s.request_id == toy.fee_op().request_id]
    assert len(fee_sends) == 1  # the killed send only: read back, never sent again
    assert engine.LEASE_RESEND_WAIT_S in clock.sleeps  # the resend waited out the lease
    [fee] = outcome.results["fee"]
    assert fee.status is OperationStatus.SUCCEEDED
    assert fee.error_code != IN_PROGRESS_CODE
    assert store.get(fee_sends[0].request_id).status == "SUCCEEDED"
