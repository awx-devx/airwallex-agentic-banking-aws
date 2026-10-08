"""The limit-boundary acceptance harness (kit 2's required acceptance test).

Pre-fund the wallet, then prove each control with a probe that breaks exactly one rule. Every
probe is a single_phase simulation, so an approval is a CLEARING in one call:

1. a disallowed currency           -> CURRENCY_NOT_ALLOWED
2. a disallowed merchant category  -> MERCHANT_CATEGORY_NOT_ALLOWED
3. the per-transaction limit + 0.01 -> LIMIT_EXCEEDED
4. exactly the per-transaction limit -> approved CLEARING (debits the wallet)
5. the smallest amount past the lowest remaining cumulative limit -> LIMIT_EXCEEDED

Probes 1-3 run before 4 so their declines cannot come from a cumulative limit already used up.
Probe 5 runs after 4, so it proves the cumulative limit.

The sandbox checks currency, merchant category and limits BEFORE funds (FAKE-FIDELITY,
Issuing: the guide says the reverse), so a probe designed to decline never reaches the funds
check. The wallet must hold the at-limit amount, the only intended debit. If a control were
missing, its probe would then reach the funds check and come back as a clear or as
INSUFFICIENT_FUNDS; either way the run fails. Any INSUFFICIENT_FUNDS decline makes the run a
harness failure, never a passed control: an empty wallet proves nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from awx_common import Money
from awx_common.workflow import PostconditionResult

from .amounts import parse_money
from .controls import (
    CUMULATIVE,
    CardControls,
    DeclineReason,
    Interval,
    SpendSoFar,
    control_violations,
)
from .policy_config import POLICY, PurchasePolicy
from .provider import TxnState


class ProbeKind(StrEnum):
    CURRENCY = "probe_currency"
    MERCHANT_CATEGORY = "probe_mcc"
    OVER_LIMIT = "probe_over_limit"
    AT_LIMIT = "probe_at_limit"
    CUMULATIVE = "probe_cumulative"


PROBE_ORDER = (
    ProbeKind.CURRENCY,
    ProbeKind.MERCHANT_CATEGORY,
    ProbeKind.OVER_LIMIT,
    ProbeKind.AT_LIMIT,
    ProbeKind.CUMULATIVE,
)


class HarnessNotPossible(ValueError):
    """The card's state cannot isolate one rule per probe; running would prove nothing."""


@dataclass(frozen=True)
class Probe:
    kind: ProbeKind
    amount: Money
    merchant_category: str
    expect: DeclineReason | None  # None: must clear

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "amount": format(self.amount.amount, "f"),
            "currency": self.amount.currency,
            "merchant_category": self.merchant_category,
            "expect": self.expect.value if self.expect else "CLEARED",
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Probe:
        expect = d["expect"]
        return cls(
            kind=ProbeKind(d["kind"]),
            amount=parse_money(d["amount"], d["currency"]),
            merchant_category=d["merchant_category"],
            expect=None if expect == "CLEARED" else DeclineReason(expect),
        )


def _spent_after(controls: CardControls, spent: SpendSoFar, amount: Money) -> SpendSoFar:
    return SpendSoFar(
        {
            lim.interval: spent.of(lim.interval, controls.currency) + amount
            for lim in controls.limits
            if lim.interval in CUMULATIVE
        }
    )


def plan_probes(
    controls: CardControls, spent: SpendSoFar, policy: PurchasePolicy = POLICY
) -> tuple[Probe, ...]:
    """Design one probe per rule and check, with the local rule model, that each one breaks
    exactly the rule it is meant to test and nothing else."""
    if controls.single_use:
        raise HarnessNotPossible(
            "a single-use card cannot isolate one rule per probe: its own transaction-count "
            "rule would confound the probes after the first authorization"
        )
    cur = controls.currency
    per_txn = controls.limit(Interval.PER_TRANSACTION)
    assert per_txn is not None  # CardControls guarantees it
    cent = Money.from_minor(1, cur)
    small = Money.of(policy.probe_small_amount, cur)
    if small > per_txn:
        small = per_txn
    allowed_mcc = controls.allowed_merchant_categories[0]
    if policy.disallowed_mcc in controls.allowed_merchant_categories:
        raise HarnessNotPossible("the policy's disallowed MCC is allowed on this card")
    other = next((c for c in policy.probe_currencies if c != cur), None)
    if other is None:
        raise HarnessNotPossible("no probe currency differs from the card currency")
    probes = [
        Probe(
            ProbeKind.CURRENCY,
            Money.of(policy.probe_small_amount, other),
            allowed_mcc,
            DeclineReason.CURRENCY_NOT_ALLOWED,
        ),
        Probe(
            ProbeKind.MERCHANT_CATEGORY,
            small,
            policy.disallowed_mcc,
            DeclineReason.MERCHANT_CATEGORY_NOT_ALLOWED,
        ),
        Probe(ProbeKind.OVER_LIMIT, per_txn + cent, allowed_mcc, DeclineReason.LIMIT_EXCEEDED),
        Probe(ProbeKind.AT_LIMIT, per_txn, allowed_mcc, None),
    ]
    after = _spent_after(controls, spent, per_txn)
    remaining = min(
        lim.amount - after.of(lim.interval, cur) for lim in controls.cumulative_limits()
    )
    cumulative_amount = remaining + cent
    if remaining.is_negative() or cumulative_amount > per_txn:
        raise HarnessNotPossible(
            "the cumulative limit cannot be isolated: after an at-limit charge the lowest "
            f"cumulative limit still has {remaining} left, more than one transaction"
        )
    probes.append(
        Probe(ProbeKind.CUMULATIVE, cumulative_amount, allowed_mcc, DeclineReason.LIMIT_EXCEEDED)
    )
    for p in probes:
        state = after if p.kind is ProbeKind.CUMULATIVE else spent
        got = control_violations(controls, p.amount, p.merchant_category, state)
        want = frozenset() if p.expect is None else frozenset({p.expect})
        if got != want:
            raise HarnessNotPossible(
                f"{p.kind.value} would break {sorted(got)}, not exactly {sorted(want)}"
            )
    return tuple(probes)


def required_funding(probes: Sequence[Probe], currency: str) -> Money:
    """The wallet balance the test needs: the amount of every probe designed to clear (only
    the at-limit probe). Limits are checked before funds, so declining probes need none."""
    total = Money.zero(currency)
    for p in probes:
        if p.expect is None and p.amount.currency == currency:
            total = total + p.amount
    return total


@dataclass(frozen=True)
class Observation:
    kind: ProbeKind
    state: TxnState | None  # None: never observed
    reason: str | None


def judge(
    probes: Sequence[Probe], observations: Mapping[ProbeKind, Observation]
) -> PostconditionResult:
    """Pass only when every probe ended exactly as designed. INSUFFICIENT_FUNDS anywhere is a
    harness failure even if the decline 'looks right'."""
    lines: list[str] = []
    funds = [
        k.value
        for k, o in observations.items()
        if o.reason == DeclineReason.INSUFFICIENT_FUNDS.value
    ]
    if funds:
        return PostconditionResult(
            False,
            "HARNESS FAILURE: INSUFFICIENT_FUNDS on "
            + ", ".join(sorted(funds))
            + ". The wallet was not funded, so these declines prove nothing about the card "
            "controls. Fund the wallet and rerun on a fresh card.",
        )
    ok = True
    for p in probes:
        o = observations.get(p.kind)
        if o is None or o.state is None:
            ok = False
            lines.append(f"{p.kind.value}: FAIL, never observed")
            continue
        if p.expect is None:
            good = o.state is TxnState.CLEARED
            want = "CLEARED"
        else:
            good = o.state is TxnState.DECLINED and o.reason == p.expect.value
            want = f"DECLINED {p.expect.value}"
        ok = ok and good
        got = o.state.value + (f" {o.reason}" if o.reason else "")
        lines.append(
            f"{p.kind.value} {p.amount}: {'PASS' if good else 'FAIL'}, want {want}, got {got}"
        )
    return PostconditionResult(ok, "; ".join(lines))
