"""Joining a card authorisation simulation to the issuing transaction it created.

simulation/issuing/create answers with `card_transaction_id` (the executor's provider_id) and
`card_transaction_lifecycle_id`. Neither is the id GET /api/v1/issuing/transactions/{id} takes:
that is the listed transaction's `transaction_id` (sandbox, 6 Oct: the simulation id gave 404).
The listed transaction carries the simulation ids under `card_transaction_data`, so join there.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awx_common import AirwallexPort, ReadRequest
from awx_common.errors import ValidationFailed

PAGE_SIZE = 100


def find_card_transaction(
    port: AirwallexPort,
    *,
    card_id: str,
    card_transaction_id: str,
    on_behalf_of: str | None = None,
    max_pages: int = 50,
) -> Mapping[str, Any] | None:
    """The /issuing/transactions item for a simulation's card_transaction_id, or None.

    Its `transaction_id` is the id GET /api/v1/issuing/transactions/{id} accepts.
    """
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValidationFailed("max_pages must be a positive int", code="invalid_argument")
    # Sandbox r2: pages are page_num from 0 with page_size >= 10 (smaller is 400
    # BELOW_MIN_PAGE_SIZE), oldest first, with has_more.
    for page in range(max_pages):
        body = port.read(
            ReadRequest(
                "/api/v1/issuing/transactions",
                {"card_id": card_id, "page_num": str(page), "page_size": str(PAGE_SIZE)},
                on_behalf_of=on_behalf_of,
            )
        )
        for item in body.get("items") or []:
            data = item.get("card_transaction_data") if isinstance(item, Mapping) else None
            if isinstance(data, Mapping) and data.get("card_transaction_id") == card_transaction_id:
                return item
        if not body.get("has_more"):
            return None
    return None
