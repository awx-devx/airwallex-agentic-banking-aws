"""Provider state maps and read parsing, kept beside the flow that uses them.

Card transactions do not finish at a status called APPROVED. A cleared REST read can stay
PENDING longer than a demo, and REST and MCP name states differently (guide, kit 2 watch-outs and
Sandbox constraints > Cards). So kit 2 never waits for APPROVED: `transaction_type: CLEARING`
means accepted and a non-empty `failure_reason` means declined. The REST and MCP maps are kept
separate; the spine reads over REST, so REST_TXN_STATE is the one in use. MCP_TXN_STATE is
documentation for a team that reads over MCP and is never mixed with the REST map.

Cards and transfers have separate state machines: a declined card transaction ends in FAILED,
a failed transfer in CANCELLED. Kit 2 has no transfers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any

from awx_common import Money, OperationResult, ReadRequest

from .amounts import parse_money
from .controls import CardControls, DeclineReason, Interval, SpendSoFar


class TxnState(StrEnum):
    """Kit 2's own transaction states, derived from a provider read."""

    PENDING = "PENDING"  # not yet decided; keep polling
    AUTHORIZED = "AUTHORIZED"  # authorization accepted, not yet cleared
    CLEARED = "CLEARED"  # accepted: transaction_type CLEARING
    DECLINED = "DECLINED"  # failure_reason present or status FAILED
    REVERSED = "REVERSED"


# REST status -> state, used only when neither failure_reason nor transaction_type decides.
REST_TXN_STATE: Mapping[str, TxnState] = {
    "PENDING": TxnState.AUTHORIZED,
    "APPROVED": TxnState.AUTHORIZED,
    "FAILED": TxnState.DECLINED,
    "REVERSED": TxnState.REVERSED,
}
# MCP status -> state (guide: after capture, MCP reports CLEARED where REST may say APPROVED).
MCP_TXN_STATE: Mapping[str, TxnState] = {
    "PENDING": TxnState.AUTHORIZED,
    "CLEARED": TxnState.CLEARED,
    "FAILED": TxnState.DECLINED,
    "REVERSED": TxnState.REVERSED,
}

TXN_TERMINAL = frozenset(
    {TxnState.AUTHORIZED, TxnState.CLEARED, TxnState.DECLINED, TxnState.REVERSED}
)

CARDHOLDER_TERMINAL = frozenset({"READY", "DISABLED", "INCOMPLETE"})
CARDHOLDER_SUCCESS = frozenset({"READY"})
CARD_TERMINAL = frozenset({"ACTIVE", "INACTIVE", "CLOSED", "BLOCKED", "LOST", "STOLEN", "FAILED"})


def rest_txn_state(body: Mapping[str, Any]) -> TxnState:
    reason = body.get("failure_reason")
    if isinstance(reason, str) and reason.strip():
        return TxnState.DECLINED
    if body.get("status") == "FAILED":
        return TxnState.DECLINED
    if body.get("transaction_type") == "CLEARING":
        return TxnState.CLEARED
    if body.get("transaction_type") == "REVERSAL":
        return TxnState.REVERSED
    return REST_TXN_STATE.get(str(body.get("status")), TxnState.PENDING)


def sim_reply_state(body: Mapping[str, Any]) -> TxnState:
    """State from the POST /simulation/issuing/create reply, which is final on 201.

    FAKE-FIDELITY (Issuing): the reply carries `process_result` APPROVED or DECLINED, a
    `failure_reason` on a decline, and `type` CLEARING for a single_phase approval or
    AUTHORIZATION for a two-phase one.
    """
    reason = body.get("failure_reason")
    if (isinstance(reason, str) and reason.strip()) or body.get("process_result") == "DECLINED":
        return TxnState.DECLINED
    if body.get("process_result") == "APPROVED":
        if body.get("type") == "CLEARING":
            return TxnState.CLEARED
        return TxnState.AUTHORIZED
    return TxnState.PENDING


def mcp_txn_state(body: Mapping[str, Any]) -> TxnState:
    reason = body.get("failure_reason")
    if isinstance(reason, str) and reason.strip():
        return TxnState.DECLINED
    if body.get("transaction_type") == "CLEARING":
        return TxnState.CLEARED
    return MCP_TXN_STATE.get(str(body.get("status")), TxnState.PENDING)


def decline_reason(body: Mapping[str, Any]) -> DeclineReason | str | None:
    """The provider's decline reason. An unknown reason is returned as the raw string."""
    reason = body.get("failure_reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    try:
        return DeclineReason(reason)
    except ValueError:
        return reason


def object_id(result: OperationResult, *keys: str) -> str:
    """The provider id of a created object, from the result or its body."""
    if result.provider_id:
        return result.provider_id
    body = result.body or {}
    for key in (*keys, "id"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError(f"result {result.request_id} carries no provider id")


def card_id_of(body: Mapping[str, Any]) -> str | None:
    for key in ("card_id", "id"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---- read requests ---------------------------------------------------------------------------


def read_card(card_id: str) -> ReadRequest:
    return ReadRequest(f"/api/v1/issuing/cards/{card_id}")


def read_card_limits(card_id: str) -> ReadRequest:
    return ReadRequest(f"/api/v1/issuing/cards/{card_id}/limits")


def read_cardholder(cardholder_id: str) -> ReadRequest:
    return ReadRequest(f"/api/v1/issuing/cardholders/{cardholder_id}")


def read_card_transactions(card_id: str) -> ReadRequest:
    return ReadRequest("/api/v1/issuing/transactions", {"card_id": card_id})


BALANCES = ReadRequest("/api/v1/balances/current")


# ---- parsing reads ---------------------------------------------------------------------------


def row_card_transaction_id(row: Mapping[str, Any]) -> str | None:
    """The simulation's card_transaction_id for a GET /issuing/transactions row.

    The row's own `transaction_id` is a different id: GET /issuing/transactions/{the
    simulation's id} is a 404 (FAKE-FIDELITY, Issuing). Join on this field instead.
    """
    data = row.get("card_transaction_data")
    if isinstance(data, Mapping):
        value = data.get("card_transaction_id")
        if isinstance(value, str) and value:
            return value
    return None


def row_amount(row: Mapping[str, Any]) -> Money | None:
    """abs(transaction_amount) of a list row; list amounts are negative (FAKE-FIDELITY)."""
    currency = row.get("transaction_currency")
    amount = row.get("transaction_amount")
    if not isinstance(currency, str) or isinstance(amount, bool | float):
        return None
    if not isinstance(amount, int | Decimal):
        return None
    return Money.of(abs(Decimal(amount)), currency)


def row_merchant(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(category_code, name) of a list row's `merchant` object."""
    merchant = row.get("merchant")
    if not isinstance(merchant, Mapping):
        return None, None
    mcc, name = merchant.get("category_code"), merchant.get("name")
    return (mcc if isinstance(mcc, str) else None, name if isinstance(name, str) else None)


def items(body: Any) -> Sequence[Mapping[str, Any]]:
    """A list endpoint's rows, whether the spine hands back a list or {"items": [...]}."""
    if isinstance(body, list):
        return body
    if isinstance(body, Mapping):
        rows = body.get("items")
        if isinstance(rows, list):
            return rows
    raise ValueError("expected a list response")


def available_balance(body: Any, currency: str) -> Money:
    """available_amount for one currency from GET /balances/current. Missing means zero."""
    for row in items(body):
        if row.get("currency") == currency:
            return parse_money(row.get("available_amount"), currency, field="available_amount")
    return Money.zero(currency)


def card_controls(card: Mapping[str, Any]) -> CardControls:
    controls = card.get("authorization_controls")
    if not isinstance(controls, Mapping):
        raise ValueError("card read has no authorization_controls")
    return CardControls.from_provider(controls)


def limits_spent(limits_body: Mapping[str, Any], currency: str) -> SpendSoFar | None:
    """Spend counted against each cumulative interval, from GET /issuing/cards/{id}/limits.

    Returns None when the read does not carry `remaining` for every limit: the spend is then
    unknown and callers fail closed.
    """
    if limits_body.get("currency") not in (None, currency):
        return None
    spent: dict[Interval, Money] = {}
    for row in limits_body.get("limits") or ():
        if "remaining" not in row:
            return None
        interval = Interval(row["interval"])
        amount = parse_money(row["amount"], currency, field="limit.amount")
        remaining = parse_money(row["remaining"], currency, field="limit.remaining")
        if interval is not Interval.PER_TRANSACTION:
            spent[interval] = amount - remaining
    return SpendSoFar(spent)


def limits_match(limits_body: Mapping[str, Any], expected: CardControls) -> list[str]:
    """Differences between the provider's limits and the approved control set."""
    problems: list[str] = []
    if limits_body.get("currency") not in (None, expected.currency):
        problems.append(f"limit currency {limits_body.get('currency')} != {expected.currency}")
    got: dict[str, Money] = {}
    for row in limits_body.get("limits") or ():
        try:
            got[str(row["interval"])] = parse_money(row["amount"], expected.currency)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"unreadable limit row: {exc}")
    want = {lim.interval.value: lim.amount for lim in expected.limits}
    for interval, amount in want.items():
        if interval not in got:
            problems.append(f"{interval} limit missing")
        elif got[interval] != amount:
            problems.append(f"{interval} limit {got[interval]} != approved {amount}")
    for interval in set(got) - set(want):
        problems.append(f"unexpected {interval} limit")
    return problems
