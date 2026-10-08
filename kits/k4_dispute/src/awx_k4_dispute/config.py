"""Kit 4 configuration: the numbers the merchant owns, not the model.

The sandbox does not deduct a dispute fee (guide, kit 4 watch-outs), so the fee and handling
costs here are MODELLED application economics. They never appear in a provider balance and the
ledger labels them that way. Every value is an AWS-authored assumption for the PoC; a real
merchant would load its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from awx_common import Money

KIT = "k4"
POLICY_VERSION = "k4-policy-2026-10-06.1"


class StrengthBand(StrEnum):
    """How strong the verified evidence is. Code derives it from typed facts (evidence.py)."""

    NONE = "NONE"
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


BAND_ORDER: dict[StrengthBand, int] = {
    StrengthBand.NONE: 0,
    StrengthBand.WEAK: 1,
    StrengthBand.MODERATE: 2,
    StrengthBand.STRONG: 3,
}


@dataclass(frozen=True)
class CurrencyTerms:
    """Per-currency economics. All Money values are in that currency; the kit never converts."""

    dispute_fee: Money  # modelled; charged when the merchant contests, win or lose
    contest_handling_cost: Money  # modelled staff time to assemble and submit evidence
    accept_handling_cost: Money  # modelled staff time to accept
    min_saving_to_contest: Money  # indifference band: smaller expected savings go to a person
    dual_approval_threshold: Money  # disputed amount at or above this needs two approvers

    def __post_init__(self) -> None:
        currencies = {
            self.dispute_fee.currency,
            self.contest_handling_cost.currency,
            self.accept_handling_cost.currency,
            self.min_saving_to_contest.currency,
            self.dual_approval_threshold.currency,
        }
        if len(currencies) != 1:
            raise ValueError("every CurrencyTerms amount must share one currency")
        for value in (
            self.dispute_fee,
            self.contest_handling_cost,
            self.accept_handling_cost,
            self.min_saving_to_contest,
            self.dual_approval_threshold,
        ):
            if value.is_negative():
                raise ValueError("CurrencyTerms amounts must not be negative")


def _terms(currency: str, fee: str, contest: str, accept: str, saving: str, dual: str):
    return CurrencyTerms(
        dispute_fee=Money.of(fee, currency),
        contest_handling_cost=Money.of(contest, currency),
        accept_handling_cost=Money.of(accept, currency),
        min_saving_to_contest=Money.of(saving, currency),
        dual_approval_threshold=Money.of(dual, currency),
    )


@dataclass(frozen=True)
class DisputeConfig:
    policy_version: str = POLICY_VERSION
    terms: Mapping[str, CurrencyTerms] = field(
        default_factory=lambda: {
            "AUD": _terms("AUD", "25.00", "20.00", "2.00", "20.00", "1000.00"),
            "USD": _terms("USD", "15.00", "15.00", "1.50", "15.00", "750.00"),
        }
    )
    # Guide: "The merchant pays a chargeback fee even after winning", and the worked example
    # accepts a small case because "the dispute fee exceeds the amount at risk". That only
    # makes sense if the fee is a cost of contesting, so by default accepting does not incur it.
    fee_on_accept: bool = False
    # Win probability per strength band. AWS-authored assumption, exact Decimals.
    win_probability: Mapping[StrengthBand, Decimal] = field(
        default_factory=lambda: {
            StrengthBand.NONE: Decimal("0"),
            StrengthBand.WEAK: Decimal("0.15"),
            StrengthBand.MODERATE: Decimal("0.45"),
            StrengthBand.STRONG: Decimal("0.75"),
        }
    )
    min_band_to_contest: StrengthBand = StrengthBand.MODERATE
    min_prior_undisputed_orders: int = 2  # Visa CE 3.0 style; the guide's strong case has 3
    # Leave time before the provider deadline: evidence upload and review take time.
    challenge_margin: timedelta = timedelta(hours=24)
    accept_margin: timedelta = timedelta(hours=1)
    # Used only when the provider read carries no deadline. Conservative on purpose.
    fallback_response_window: Mapping[str, timedelta] = field(
        default_factory=lambda: {
            "RFI": timedelta(days=5),
            "PRE_CHARGEBACK": timedelta(days=5),
            "CHARGEBACK": timedelta(days=10),
            "PRE_ARBITRATION": timedelta(days=5),
            "ARBITRATION": timedelta(days=5),
        }
    )
    default_fallback_window: timedelta = timedelta(days=3)
    approval_ttl: timedelta = timedelta(hours=12)
    # A lock whose owner never recorded a response attempt (it only uploaded evidence, which
    # moves no money) may be taken over after this long (cases.py).
    upload_lock_ttl: timedelta = timedelta(hours=1)
    # Sent on every challenge; Airwallex requires product_type at CHARGEBACK. Code-owned. The
    # enum value is unconfirmed (no live challenge yet; see DESIGN.md).
    product_type: str = "PHYSICAL_GOODS"
    product_description: str = "poc-k4 synthetic online order of physical goods"
    max_evidence_files: int = 10
    max_evidence_bytes: int = 10 * 1024 * 1024  # assumption; the guide states no limit
    max_narrative_chars: int = 2000

    def __post_init__(self) -> None:
        for currency, terms in self.terms.items():
            if terms.dispute_fee.currency != currency:
                raise ValueError(f"terms for {currency} are in {terms.dispute_fee.currency}")
        probabilities = [self.win_probability[b] for b in BAND_ORDER]
        if any(not Decimal(0) <= p <= Decimal(1) for p in probabilities):
            raise ValueError("win probabilities must lie in [0, 1]")
        if probabilities != sorted(probabilities):
            raise ValueError("win probability must not fall as evidence strengthens")
        if self.min_prior_undisputed_orders < 1:
            raise ValueError("min_prior_undisputed_orders must be at least 1")

    def terms_for(self, currency: str) -> CurrencyTerms | None:
        return self.terms.get(currency)


DEFAULT_CONFIG = DisputeConfig()
