"""Poll an object until its provider status is terminal.

The create response is never treated as final: NEW, SCHEDULED, PROCESSING and SENT are all
in flight. Terminal vocabularies are per object type and come from the kit's FinalitySpec
(transfers end at PAID or CANCELLED, platform movements at SETTLED, card declines at FAILED).
Retryable read errors are tolerated until the deadline; any other read error propagates.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from awx_common import AirwallexPort, OperationResult, OperationStatus
from awx_common.errors import AwxError, NotFound
from awx_common.workflow import FinalitySpec

log = logging.getLogger("awx_spine.finality")


class FinalityTimeout(Exception):
    def __init__(
        self, message: str, *, last_status: str | None, last_body: Mapping[str, Any] | None
    ) -> None:
        super().__init__(message)
        self.last_status = last_status
        self.last_body = last_body


def poll_until_terminal(
    port: AirwallexPort,
    result: OperationResult,
    spec: FinalitySpec,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    not_found_grace_s: float = 10.0,
) -> tuple[str, Mapping[str, Any]]:
    """Return (terminal provider status, the body it was read from).

    A NotFound within not_found_grace_s of the first poll is retried, because a read can lag
    the create; after that it propagates.
    """
    if result.status is not OperationStatus.SUCCEEDED:
        raise ValueError(f"cannot poll an operation whose result is {result.status}")
    request = spec.read_request(result)
    started = clock()
    deadline = started + spec.timeout_s
    last_status: str | None = None
    last_body: Mapping[str, Any] | None = None
    while True:
        try:
            body = port.read(request)
        except AwxError as err:
            lagging = isinstance(err, NotFound) and clock() - started < not_found_grace_s
            if not (err.retryable or lagging):
                raise
            log.info(
                "finality read of %s failed (%s); will retry", request.path, type(err).__name__
            )
        else:
            # An optional select picks this operation's row out of a list read;
            # None means not visible yet, a non-terminal poll.
            select = getattr(spec, "select", None)
            picked = body if select is None else select(body, result)
            if picked is not None:
                status = spec.status_of(picked)
                last_status, last_body = status, picked
                if status in spec.terminal:
                    return status, picked
        if clock() + spec.interval_s > deadline:
            raise FinalityTimeout(
                f"{request.path} not terminal after {spec.timeout_s}s (last status {last_status})",
                last_status=last_status,
                last_body=last_body,
            )
        sleep(spec.interval_s)
