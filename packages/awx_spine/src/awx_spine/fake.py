"""FakeAirwallex: an in-memory Airwallex sandbox for kit tests.

It implements AirwallexPort and FileUploadPort, and answers with the same HTTP statuses, error
codes and body shapes the sandbox returned on 6 Oct 2026 (FAKE-FIDELITY.md cites each one).
Responses go through the same normalisation as AirwallexClient, and every body round-trips
through the wire encoder, so a kit sees Decimal amounts, {"items": [...]} for top-level lists,
and identical OperationResults from both.

Simulated time: nothing moves until advance(seconds). Transfers go SCHEDULED -> PROCESSING after
1 s and then wait for a simulation call; platform movements go NEW -> SETTLED after 1 s; a newly
activated connected account refuses FX with unconfigured_client_fee until fx_ready_delay_s.

Faults: inject(name, fault, times=n). `name` is an op name for writes (see
awx_spine.endpoints.ENDPOINTS) or a read name for GETs (READ_NAMES below, e.g. "transfers.list").
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from awx_common import (
    PLATFORM_BODY_FIELDS,
    Money,
    Operation,
    OperationResult,
    ReadRequest,
    minor_units,
)
from awx_common.errors import OutcomeUnknown, TenantScopeViolation, ValidationFailed

from .endpoints import endpoint_for
from .normalise import error_from_response, result_from_error, result_from_response
from .wire import decode_json, encode_json

EPOCH = datetime(2026, 10, 6, tzinfo=UTC)
PLATFORM = "__platform__"
PLATFORM_ACCOUNT_UUID = "00000000-0000-4000-8000-00000000f00d"


class Fault(StrEnum):
    TIMEOUT_AFTER_SUBMIT = "TIMEOUT_AFTER_SUBMIT"
    DUPLICATE_DELIVERY = "DUPLICATE_DELIVERY"
    STALE_READ = "STALE_READ"
    RATE_LIMIT = "RATE_LIMIT"
    SERVER_ERROR = "SERVER_ERROR"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    REFUSE = "REFUSE"  # a definitive 400 for any write; nothing is written (A)
    END_AS = "END_AS"  # a platform movement ends final_status instead of SETTLED (A)
    LIST_LAG = "LIST_LAG"  # the write succeeds; its objects stay invisible for visible_after_s


# Writes whose sandbox answer to a short balance is known (FAKE-FIDELITY.md, Errors and Issuing).
INSUFFICIENT_FUNDS_OPS = frozenset(
    {
        "transfers.create",
        "charges.create",
        "connected_account_transfers.create",
        "fx.conversions.create",
        "issuing.simulate_authorization",
    }
)
READ_FAULTS = frozenset({Fault.STALE_READ, Fault.RATE_LIMIT, Fault.SERVER_ERROR})
PLATFORM_MOVES = frozenset({"charges.create", "connected_account_transfers.create"})
MOVEMENT_END_STATES = frozenset({"SUSPENDED", "PENDING", "FAILED"})
MOVEMENT_KINDS = ("charges", "connected_account_transfers")
WRITE_FAULTS = frozenset(Fault) - {Fault.STALE_READ}

# Sandbox values (6 Oct) and AWS-authored stand-ins, all documented in FAKE-FIDELITY.md.
USD_PRICE: dict[str, Decimal] = {  # synthetic: USD value of one unit
    "USD": Decimal("1"),
    "EUR": Decimal("1.122"),
    "GBP": Decimal("1.33"),
    "AUD": Decimal("0.66"),
    "CAD": Decimal("0.73"),
    "SGD": Decimal("0.77"),
    "HKD": Decimal("0.128"),
    "NZD": Decimal("0.60"),
    "JPY": Decimal("0.0067"),
    "CNY": Decimal("0.14"),
    "CHF": Decimal("1.25"),
}
CLIENT_MARKUP = Decimal("0.005")  # observed client vs awx spread on EURUSD was about 0.5 %
FX_LIMIT_USD = Decimal("1000000")  # assumption: 50,000,000 USD hit amount_above_limit
SWIFT_FEE = {"EUR": Decimal("6.21")}  # observed EUR 6.21 for 1 and 100 EUR (guide says 12.85)
DEFAULT_SWIFT_FEE = Decimal("6.21")  # assumption for other currencies
TRANSFER_REASONS_ANY = True  # transfers/create accepted an arbitrary reason string
PLATFORM_REASONS = frozenset(
    {
        "goods_purchased",
        "professional_business_services",
        "technical_services",
        "freight",
        "taxes",
        "transfer_to_own_account",
        "education_training",
        "loan_credit_repayment",
        "investment_proceeds",
        "other_services",
        "medical_services",
        "donation_charitable_contribution",
        "personal_remittance",
        "living_expenses",
        "investment_capital",
        "pension",
        "construction",
        "business_expenses",
        "wages_salary",
        "travel",
        "family_support",
        "real_estate",
    }
)
FIRST_VALIDATOR_EXTRA = frozenset({"settlement", "commission"})  # listed, then rejected
TRANSFER_FAILURE_TYPES = frozenset(
    {
        "ACCOUNT_CLOSED",
        "ACCOUNT_CURRENCY_MISMATCH",
        "ACCOUNT_INACTIVE_OR_DORMANT",
        "ACCOUNT_UNDER_RESTRICTION",
        "BENEFICIARY_BANK_RETURNED",
        "BENEFICIARY_NAME_MISMATCH",
        "BENEFICIARY_REQUESTED",
        "CARD_ISSUER_ERROR",
        "CHANNEL_POLICY",
        "CHANNEL_TIMEOUT",
        "CLIENT_REQUESTED",
        "DUPLICATION_RETURN",
        "EXCEEDED_TRANSACTION_AMOUNT_OR_LIMIT",
        "INBOUND_2B_CARD_NOT_SUPPORTED",
        "INBOUND_BENFICIARY_VALIDATION_ERROR",
        "INBOUND_COMPANY_REGISTRATION_NUMBER_ERROR",
        "INBOUND_ORDER_ERROR",
        "INSUFFICIENT_FUNDS",
        "INVALID_ACCOUNT_NAME_OR_NUMBER",
        "INVALID_BANK_INFORMATION",
        "INVALID_BANK_OR_BRANCH_CODE",
        "INVALID_BENEFICIARY_DETAILS",
        "INVALID_CORRESPONDENT_BANK_INFORMATION",
        "INVALID_PAYMENT_PURPOSE",
        "INVALID_SPECIAL_CHARACTER",
        "INVALID_SWIFT_BIC_CODE",
        "OTHER",
        "RECALL_REQUESTED",
        "SYSTEM_ERROR",
        "TM_SUSPENDED",
        "TRANSACTION_AMOUNT_NOT_COVERING_FEE",
        "UNABLE_TO_APPLY",
    }
)
# failure_type -> (failure.code, failure.message); first two observed, OTHER observed for a
# FAILED transition sent without failure_type. The rest are AWS-authored stand-ins.
FAILURE_CODES = {
    "BENEFICIARY_BANK_RETURNED": ("90802", "Beneficiary bank returned"),
    "ACCOUNT_CLOSED": ("90701", "Account closed"),
    None: ("99902", "Other"),
}
CARD_SIM_CURRENCIES = frozenset(
    {"USD", "AUD", "GBP", "EUR", "CAD", "JPY", "HKD", "CNY", "SGD", "NZD", "CHF"}
)
LIMIT_INTERVALS = frozenset({"PER_TRANSACTION", "DAILY", "WEEKLY", "MONTHLY", "ALL_TIME"})
TEST_CARD = "4035501000000008"
DISPUTE_REASON_CODES = frozenset({"10.4", "13.1", "13.6"})
ACCEPT_REASONS = frozenset(
    {
        "AGREEMENT_REACHED_WITH_CUSTOMER",
        "CUSTOMER_RELATIONSHIP_MAINTENANCE",
        "LOW_VALUE_TRANSACTION",
        "VALID_CUSTOMER_DISPUTE",
        "COLLABORATION_ACCEPTED_MANUAL",
        "OTHERS",
    }
)
CHALLENGE_REASONS = frozenset(
    {
        "CUSTOMER_WITHDRAWN",
        "CUSTOMER_REFUNDED",
        "PRODUCT_RECEIVED",
        "PURCHASE_HISTORY",
        "NOT_ENTITLED",
        "SEPARATE_PRODUCT",
        "AUTHENTIC_PRODUCT",
        "OTHER_REASONS",
    }
)


class _Reply(Exception):
    """An HTTP reply raised from deep inside a handler."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


def _err(status: int, code: str, message: str, **extra: Any) -> _Reply:
    return _Reply(status, {"code": code, "message": message, **extra})


def _corr_item(seq: int) -> list[dict[str, str]]:
    return [
        {"code": "correlation_id", "message": str(uuid.uuid5(uuid.NAMESPACE_URL, f"corr:{seq}"))}
    ]


def _num(value: Decimal) -> Decimal | int:
    """Amounts as the provider writes them: 25, 10.0 style normalisation, no trailing zeros."""
    if value == value.to_integral_value():
        return int(value)
    return value.normalize()


def _dec(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise _err(400, "validation_failed", f"field [{field_name}] is required", source=field_name)
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError:
            pass
    raise _err(400, "validation_failed", f"{field_name} is invalid", source=field_name)


def _fits(amount: Decimal, currency: str) -> bool:
    return amount == amount.quantize(Decimal(1).scaleb(-minor_units(currency)))


@dataclass
class _Account:
    id: str
    nickname: str
    status: str
    details: dict[str, Any]
    balances: dict[str, list[Decimal]] = field(default_factory=dict)  # ccy -> [available, reserved]
    activated_at: float | None = None
    has_ein: bool = True

    def bal(self, ccy: str) -> list[Decimal]:
        return self.balances.setdefault(ccy, [Decimal(0), Decimal(0)])


@dataclass
class _State:
    accounts: dict[str, _Account] = field(default_factory=dict)
    objects: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # kind -> id -> obj
    # (owner, kind, request_id) -> (object id, fake time the request_id was first used)
    request_ids: dict[tuple[str, str, str], tuple[str, float]] = field(default_factory=dict)

    def kind(self, name: str) -> dict[str, dict[str, Any]]:
        return self.objects.setdefault(name, {})


READ_NAMES: tuple[tuple[str, str], ...] = (
    ("/api/v1/balances/current", "balances.current"),
    ("/api/v1/transfers", "transfers.list"),
    ("/api/v1/transfers/{id}", "transfers.get"),
    ("/api/v1/beneficiaries", "beneficiaries.list"),
    ("/api/v1/pa/refunds", "refunds.list"),
    ("/api/v1/balances/history", "balances.history"),
    ("/api/v1/beneficiaries/{id}", "beneficiaries.get"),
    ("/api/v1/fx/rates/current", "fx.rates.current"),
    ("/api/v1/fx/conversions", "fx.conversions.list"),
    ("/api/v1/fx/conversions/{id}", "fx.conversions.get"),
    ("/api/v1/global_accounts", "global_accounts.list"),
    ("/api/v1/global_accounts/{id}", "global_accounts.get"),
    ("/api/v1/accounts", "accounts.list"),
    ("/api/v1/accounts/{id}", "accounts.get"),
    ("/api/v1/connected_account_transfers", "connected_account_transfers.list"),
    ("/api/v1/connected_account_transfers/{id}", "connected_account_transfers.get"),
    ("/api/v1/charges", "charges.list"),
    ("/api/v1/charges/{id}", "charges.get"),
    ("/api/v1/issuing/cardholders", "issuing.cardholders.list"),
    ("/api/v1/issuing/cardholders/{id}", "issuing.cardholders.get"),
    ("/api/v1/issuing/cards", "issuing.cards.list"),
    ("/api/v1/issuing/cards/{id}", "issuing.cards.get"),
    ("/api/v1/issuing/cards/{id}/limits", "issuing.cards.limits"),
    ("/api/v1/issuing/transactions", "issuing.transactions.list"),
    ("/api/v1/issuing/transactions/{id}", "issuing.transactions.get"),
    ("/api/v1/pa/payment_intents", "payment_intents.list"),
    ("/api/v1/pa/payment_intents/{id}", "payment_intents.get"),
    ("/api/v1/pa/payment_disputes", "disputes.list"),
    ("/api/v1/pa/payment_disputes/{id}", "disputes.get"),
    ("/api/v1/platform_reports/{id}", "platform_reports.get"),
    ("/api/v1/financial_transactions", "financial_transactions.list"),
)


def _split(path: str) -> list[str]:
    return path.strip("/").split("/")


def _match(template: str, path: str) -> list[str] | None:
    t, p = _split(template), _split(path)
    if len(t) != len(p):
        return None
    ids: list[str] = []
    for a, b in zip(t, p, strict=True):
        if a == "{id}":
            ids.append(b)
        elif a != b:
            return None
    return ids


def read_name(path: str) -> str | None:
    for template, name in READ_NAMES:
        if _match(template, path) is not None:
            return name
    return None


class FakeAirwallex:
    """In-memory sandbox. See the module docstring and FAKE-FIDELITY.md."""

    def __init__(
        self,
        *,
        balances: Mapping[str, Money] | None = None,
        fx_ready_delay_s: int = 180,
        admin_key: bool = False,
        online_payments_enabled: bool = False,
        usd_price: Mapping[str, Decimal] | None = None,
        raw_platform_headers: bool = False,
        list_lag_s: float = 0.0,
        instance_id: str | None = None,
        nickname_filter: bool = False,
        dedupe_window_s: float = 7 * 86400,
    ) -> None:
        self._now = 0.0
        self._seq = 0
        self._fx_ready_delay_s = fx_ready_delay_s
        self._admin_key = admin_key
        self._raw_platform_headers = raw_platform_headers
        if list_lag_s < 0 or dedupe_window_s <= 0:
            raise ValueError("list_lag_s must be >= 0 and dedupe_window_s > 0")
        # Every create stays out of reads for list_lag_s (sandbox: 0.65 to 1.9 s observed).
        self._list_lag_s = float(list_lag_s)
        # Guide: request_ids are deduplicated for 7 days (G, not sandbox-verified).
        self._dedupe_window_s = float(dedupe_window_s)
        self._online_payments = online_payments_enabled
        self._usd_price = dict(usd_price or USD_PRICE)
        # Ids are unique across instances (a fresh fake never reuses another's transfer
        # id); pass instance_id for byte-identical runs.
        self._instance = instance_id or uuid.uuid4().hex
        # The sandbox did NOT filter GET /beneficiaries by ?nickname= (a list read after the
        # r1-ben-* probes; not in the evidence logs). Off by default so a kit that trusts the
        # server filter fails here as it would live.
        self._nickname_filter = nickname_filter
        self._ledger: list[dict[str, Any]] = []  # balances/history and deposits (A)
        self._taken_extra: dict[str, Any] = {}
        self._state = _State()
        self._previous = copy.deepcopy(self._state)
        self._faults: dict[str, list[list[Any]]] = {}
        self.requests: list[tuple[str, str, str | None]] = []  # (method, path, on_behalf_of)
        platform = _Account(PLATFORM, "platform", "ACTIVE", {}, activated_at=-1e9)
        self._state.accounts[PLATFORM] = platform
        for key, money in (balances or {}).items():
            if key != money.currency:
                raise ValueError("balances must be keyed by currency")
            platform.bal(money.currency)[0] += money.amount
            self._post(PLATFORM, money.currency, money.amount, "OPENING_BALANCE", None)
        self._previous = copy.deepcopy(self._state)

    # ---- test controls ------------------------------------------------------------------------

    def inject(
        self,
        op_name: str,
        fault: Fault,
        *,
        times: int = 1,
        applied: bool = False,
        visible_after_s: float | None = None,
        code: str = "validation_failed",
        final_status: str | None = None,
    ) -> None:
        """Queue a fault for the next `times` calls of op_name.

        visible_after_s (TIMEOUT_AFTER_SUBMIT and LIST_LAG): every object the write creates stays
        out of list, ?request_id= and GET-by-id reads until the fake clock is visible_after_s past
        the write, whatever else is written meanwhile (M1's landed-but-invisible write).
        Deduplication sees it at once, so a resend inside the lag gets the duplicate answer.

        applied=True (SERVER_ERROR on a write only) performs the write and then answers 500,
        as the sandbox did for a simulation transition ([tr3-sent]); on other writes that is an
        AWS-authored extrapolation (A). INSUFFICIENT_FUNDS exists
        only where the sandbox has an insufficient-funds answer (INSUFFICIENT_FUNDS_OPS).
        """
        fault = Fault(fault)
        is_read = any(name == op_name for _, name in READ_NAMES)
        if is_read and fault not in READ_FAULTS:
            raise ValueError(f"{fault} does not apply to the read {op_name}")
        if not is_read and fault not in WRITE_FAULTS:
            raise ValueError(f"{fault} applies to reads; name a read such as transfers.list")
        if not is_read and op_name not in _write_names():
            raise ValueError(f"unknown op name {op_name!r}")
        if fault is Fault.END_AS and (
            op_name not in PLATFORM_MOVES or final_status not in MOVEMENT_END_STATES
        ):
            raise ValueError(
                "END_AS needs a platform movement and final_status in "
                f"{sorted(MOVEMENT_END_STATES)}"
            )
        if fault is Fault.INSUFFICIENT_FUNDS and op_name not in INSUFFICIENT_FUNDS_OPS:
            raise ValueError(f"{op_name} has no insufficient-funds answer in the sandbox")
        if applied and (is_read or fault is not Fault.SERVER_ERROR):
            raise ValueError("applied=True is only for SERVER_ERROR on a write")
        lag_faults = (Fault.TIMEOUT_AFTER_SUBMIT, Fault.LIST_LAG)
        if fault is Fault.LIST_LAG and visible_after_s is None:
            raise ValueError("LIST_LAG needs visible_after_s")
        if visible_after_s is not None and (fault not in lag_faults or visible_after_s < 0):
            raise ValueError("visible_after_s >= 0 is only for TIMEOUT_AFTER_SUBMIT or LIST_LAG")
        if times < 1:
            raise ValueError("times must be at least 1")
        self._faults.setdefault(op_name, []).append(
            [fault, times, applied, visible_after_s, {"code": code, "final_status": final_status}]
        )

    def advance(self, seconds: float) -> None:
        """Move the fake clock. Wire MutationExecutor(sleep=fake.advance) and
        poll_until_terminal(clock=fake.clock, sleep=fake.advance) so lags and holds elapse."""
        if seconds < 0:
            raise ValueError("time only moves forward")
        self._now += seconds
        st = self._state
        for t in st.kind("transfers").values():
            if t["status"] == "SCHEDULED" and self._now - t["_t"] >= 1:
                t["status"] = "PROCESSING"
        for kind in ("connected_account_transfers", "charges"):
            for obj in st.kind(kind).values():
                if obj["status"] == "NEW" and self._now - obj["_t"] >= 1:
                    end = obj.pop("_end_as", None)
                    if end is None:
                        obj["status"] = "SETTLED"
                        obj["updated_at"] = self._ts()
                    else:
                        try:
                            self._end_movement(kind, obj, end)
                        except _UnrecoverableReversal:
                            # Fake limit: the money was spent, so it ends SUSPENDED (no reversal).
                            self._end_movement(kind, obj, "SUSPENDED")
        for ch in st.kind("cardholders").values():
            if ch["status"] == "PENDING" and self._now - ch["_t"] >= 2:
                ch["status"] = "READY"
        for rep in st.kind("platform_reports").values():
            if rep["status"] == "PENDING" and self._now - rep["_t"] >= 5:
                rep["status"] = "COMPLETED"
                rep["download_url"] = f"https://files.sandbox.airwallex.com/fake/{rep['id']}"

    # ---- public test controls -------------------------------------------------------------

    def set_price(self, currency: str, usd_price: Decimal | str) -> None:
        """Set the synthetic USD value of one unit of `currency` (A: prices are synthetic)."""
        value = Decimal(usd_price)
        if value <= 0:
            raise ValueError("usd_price must be positive")
        self._usd_price[currency] = value

    def price(self, currency: str) -> Decimal:
        return self._usd_price[currency]

    def credit(
        self, currency: str, amount: Decimal | str | Money, account: str | None = None
    ) -> Money:
        """Add (or, with a negative amount, remove) available funds outside any API call: a
        payment the customer made elsewhere, or a top-up. None is the platform account. Never
        takes available below zero. Returns the new available balance. Recorded in the ledger
        as ADJUSTMENT (A)."""
        acct = self._state.accounts.get(account or PLATFORM)
        if acct is None:
            raise ValueError(f"unknown account {account!r}")
        value = amount.amount if isinstance(amount, Money) else Decimal(amount)
        bal = acct.bal(currency)
        if bal[0] + value < 0:
            raise ValueError("credit would take available below zero")
        self._mutate()
        bal[0] += value
        self._post(acct.id, currency, value, "ADJUSTMENT", None)
        return Money.of(bal[0], currency)

    def reserve(self, currency: str, amount: Decimal | str, account: str | None = None) -> None:
        """Move available funds to reserved (as a pending card authorisation does)."""
        acct = self._state.accounts[account or PLATFORM]
        value = Decimal(amount)
        bal = acct.bal(currency)
        if value <= 0 or bal[0] < value:
            raise ValueError("cannot reserve that amount")
        self._mutate()
        bal[0] -= value
        bal[1] += value
        self._post(acct.id, currency, -value, "RESERVE", None)

    def clear_faults(self, op_name: str | None = None) -> None:
        """Drop queued faults for one op or read name, or all of them."""
        if op_name is None:
            self._faults.clear()
        else:
            self._faults.pop(op_name, None)

    def find(self, kind: str, request_id: str) -> dict[str, Any] | None:
        """The public view of the object of `kind` created under request_id, visible or not."""
        for obj in self._state.kind(kind).values():
            if obj.get("request_id") == request_id:
                return self._public(obj)
        return None

    def set_movement_status(self, request_id: str, status: str) -> dict[str, Any]:
        """End a charge or connected account transfer SUSPENDED, PENDING or FAILED, as an
        Airwallex operator would (the sandbox cannot provoke these; A). FAILED gives the money
        back to the payer. Returns the movement's public view."""
        if status not in MOVEMENT_END_STATES:
            raise ValueError(f"status must be one of {sorted(MOVEMENT_END_STATES)}")
        for kind in MOVEMENT_KINDS:
            for obj in self._state.kind(kind).values():
                if obj.get("request_id") == request_id:
                    if status == "FAILED" and obj["status"] != "FAILED":
                        dst = self._state.accounts[obj["_dst"]]
                        if dst.bal(obj["currency"])[0] < Decimal(obj["amount"]):
                            raise ValueError(
                                "the destination no longer holds the amount; a FAILED reversal "
                                "would take it below zero (fake limit)"
                            )
                    self._mutate()
                    self._end_movement(kind, obj, status)
                    return self._public(obj)
        raise KeyError(request_id)

    def _end_movement(self, kind: str, obj: dict[str, Any], status: str) -> None:
        if status == "FAILED" and obj["status"] != "FAILED":
            ccy, amount = obj["currency"], Decimal(obj["amount"])
            src = self._state.accounts[obj["_src"]]
            dst = self._state.accounts[obj["_dst"]]
            if dst.bal(ccy)[0] < amount:
                # The destination has spent the money. What Airwallex does then is unknown (A),
                # so the fake never invents a negative balance: see FAKE-FIDELITY "Fake limits".
                raise _UnrecoverableReversal(obj["id"])
            dst.bal(ccy)[0] -= amount
            src.bal(ccy)[0] += amount
            self._post(dst.id, ccy, -amount, "REVERSAL", obj["id"])
            self._post(src.id, ccy, amount, "REVERSAL", obj["id"])
            obj["failure_reasons"] = ["OTHER_HIGH_RISK_PAYER"]
        obj["status"] = status
        obj["updated_at"] = self._ts()

    def _post(
        self, account: str, currency: str, amount: Decimal, kind: str, source_id: str | None
    ) -> None:
        self._ledger.append(
            {
                "id": self._uuid("ledger"),
                "account": account,
                "currency": currency,
                "amount": amount,
                "transaction_type": kind,
                "source_id": source_id,
                "t": self._now,
            }
        )

    def clock(self) -> float:
        """The fake clock in seconds (for poll_until_terminal(clock=...))."""
        return self._now

    def add_connected_account(self, name: str, balances: Mapping[str, Money]) -> str:
        """An ACTIVE connected account with an EIN, FX already configured, and these balances."""
        acct = _Account(
            self._acct_id(),
            name,
            "ACTIVE",
            {"business_details": {"business_name": name}},
            activated_at=self._now - self._fx_ready_delay_s,
        )
        for key, money in balances.items():
            if key != money.currency:
                raise ValueError("balances must be keyed by currency")
            acct.bal(money.currency)[0] += money.amount
            self._post(acct.id, money.currency, money.amount, "OPENING_BALANCE", None)
        self._state.accounts[acct.id] = acct
        return acct.id

    def balance(self, currency: str, account: str | None = None) -> Money:
        """Available balance, for test assertions."""
        acct = self._state.accounts[account or PLATFORM]
        return Money.of(acct.bal(currency)[0], currency)

    def objects(self, kind: str) -> list[dict[str, Any]]:
        """Stored objects of one kind (transfers, conversions, charges, ...), oldest first."""
        return [self._public(o) for o in self._state.kind(kind).values()]

    def simulate(self, kind: str, **kwargs: Any) -> Mapping[str, Any]:
        """Sandbox simulations without building an Operation. Raises AwxError on refusal.

        kinds: deposit(global_account_id, amount, on_behalf_of=None);
        transfer_transition(transfer_id, next_status, failure_type=None, on_behalf_of=None);
        card_authorization(card_id, amount, currency, merchant_category_code="5734",
        single_phase=False, on_behalf_of=None); account_status(account_id, next_status);
        dispute(payment_intent_id, stage, reason_code, due_at); dispute_escalate(dispute_id);
        dispute_resolve(dispute_id, outcome).
        """
        obo = kwargs.pop("on_behalf_of", None)
        routes: dict[str, Callable[[], tuple[str, dict[str, Any]]]] = {
            "deposit": lambda: ("/api/v1/simulation/deposit/create", kwargs),
            "transfer_transition": lambda: (
                f"/api/v1/simulation/transfers/{kwargs.pop('transfer_id')}/transition",
                kwargs,
            ),
            "card_authorization": lambda: (
                "/api/v1/simulation/issuing/create",
                {
                    "card_id": kwargs["card_id"],
                    "transaction_amount": kwargs["amount"],
                    "transaction_currency": kwargs["currency"],
                    "merchant_category_code": kwargs.get("merchant_category_code", "5734"),
                    "merchant_info": kwargs.get("merchant_info", "poc-fake"),
                    "single_phase": kwargs.get("single_phase", False),
                },
            ),
            "account_status": lambda: (
                f"/api/v1/simulation/accounts/{kwargs.pop('account_id')}/update_status",
                {"force": True, **kwargs},
            ),
            "dispute": lambda: ("/api/v1/simulation/pa/payment_disputes/create", kwargs),
            "dispute_escalate": lambda: (
                f"/api/v1/simulation/pa/payment_disputes/{kwargs.pop('dispute_id')}/escalate",
                kwargs,
            ),
            "dispute_resolve": lambda: (
                f"/api/v1/simulation/pa/payment_disputes/{kwargs.pop('dispute_id')}/resolve",
                kwargs,
            ),
        }
        if kind not in routes:
            raise ValueError(f"unknown simulation {kind!r}")
        path, body = routes[kind]()
        status, reply = self._dispatch("POST", path, _roundtrip(body), obo)
        if not 200 <= status < 300:
            spec = endpoint_for("", path)
            raise error_from_response("POST", status, reply, op_name=spec.name if spec else None)
        return reply

    # ---- AirwallexPort ------------------------------------------------------------------------

    def execute(self, op: Operation) -> OperationResult:
        body = _roundtrip(op.body)  # floats and Money raise exactly as AirwallexClient does
        if (
            op.on_behalf_of is not None
            and op.path in PLATFORM_BODY_FIELDS
            and not self._raw_platform_headers
        ):
            # Same guard as AirwallexClient.execute: nothing is sent. The raw sandbox behaviour
            # (charges refuse it; connected account transfers make the header's account pay) is
            # behind FakeAirwallex(raw_platform_headers=True).
            return result_from_error(
                op,
                TenantScopeViolation(
                    f"{op.name} names its connected account in the body; refusing x-on-behalf-of"
                ),
            )
        self.requests.append((op.method, op.path, op.on_behalf_of))
        fault, applied, lag = self._take_full(op.name)
        before = self._existing()
        if fault is Fault.RATE_LIMIT:
            return result_from_response(
                op, 429, {"code": "too_many_requests", "message": "Too many requests"}
            )
        if fault is Fault.SERVER_ERROR:
            if applied:
                self._dispatch(op.method, op.path, body, op.on_behalf_of)
                self._hide_new(before, None)
            return result_from_response(
                op, 500, {"code": "operation_failed", "message": "Request could not be processed"}
            )
        if fault is Fault.REFUSE:
            return result_from_response(
                op,
                400,
                {
                    "code": self._taken_extra.get("code") or "validation_failed",
                    "message": "refused (injected)",
                    "source": "",
                },
            )
        if fault is Fault.END_AS:
            body = {**body, "_end_as": self._taken_extra["final_status"]}
        if fault is Fault.INSUFFICIENT_FUNDS:
            if op.name == "issuing.simulate_authorization":
                # The sandbox declines a card short of funds with 201 DECLINED, not an error.
                body = {**body, "_forced_failure": "INSUFFICIENT_FUNDS"}
            else:
                status, reply = _insufficient(op.name, self._corr())
                return result_from_response(op, status, reply)
        status, reply = self._dispatch(op.method, op.path, body, op.on_behalf_of)
        if fault is Fault.DUPLICATE_DELIVERY:
            status, reply = self._dispatch(op.method, op.path, body, op.on_behalf_of)
        self._hide_new(before, lag)
        if fault is Fault.TIMEOUT_AFTER_SUBMIT:
            return result_from_error(
                op, OutcomeUnknown(f"{op.name}: no response after the request was sent")
            )
        return result_from_response(op, status, _roundtrip(reply))

    def read(self, req: ReadRequest) -> Mapping[str, Any]:
        self.requests.append(("GET", req.path, req.on_behalf_of))
        name = read_name(req.path)
        fault = self._take(name) if name else None
        if fault is Fault.RATE_LIMIT:
            raise error_from_response(
                "GET", 429, {"code": "too_many_requests", "message": "Too many requests"}
            )
        if fault is Fault.SERVER_ERROR:
            raise error_from_response(
                "GET", 500, {"code": "internal_error", "message": "Service unavailable"}
            )
        state = self._previous if fault is Fault.STALE_READ else self._state
        current, self._state = self._state, state
        try:
            status, reply = self._get(req.path, dict(req.query), req.on_behalf_of)
        finally:
            self._state = current
        reply = _roundtrip(reply)
        if not 200 <= status < 300:
            raise error_from_response("GET", status, reply)
        if isinstance(reply, list):
            return {"items": reply}
        return reply

    def upload(
        self,
        data: bytes,
        *,
        file_name: str,
        content_type: str,
        notes: str | None = None,
        on_behalf_of: str | None = None,
    ) -> Mapping[str, Any]:
        self.requests.append(("POST", "/api/v1/files/upload", on_behalf_of))
        fault, applied, lag = self._take_full("files.upload")
        before = self._existing()
        if fault is Fault.RATE_LIMIT:
            raise error_from_response("POST", 429, {"code": "too_many_requests", "message": "x"})
        if fault is Fault.SERVER_ERROR and not applied:
            raise error_from_response("POST", 500, {"code": "internal_error", "message": "x"})
        try:
            self._owner(on_behalf_of)
        except _Reply as reply:
            raise error_from_response("POST", reply.status, reply.body) from None
        if not data:
            raise ValidationFailed("file is empty", code="invalid_argument", http_status=400)
        reply_obj = self._store_file(data, file_name, content_type, notes, on_behalf_of)
        if fault is Fault.DUPLICATE_DELIVERY:
            reply_obj = self._store_file(data, file_name, content_type, notes, on_behalf_of)
        self._hide_new(before, lag)
        if fault is Fault.SERVER_ERROR:
            raise error_from_response("POST", 500, {"code": "internal_error", "message": "x"})
        if fault is Fault.TIMEOUT_AFTER_SUBMIT:
            raise OutcomeUnknown("file upload: no response after the request was sent")
        return _roundtrip(self._public(reply_obj))

    def _store_file(
        self,
        data: bytes,
        file_name: str,
        content_type: str,
        notes: str | None,
        on_behalf_of: str | None,
    ) -> dict[str, Any]:
        file_id = _b64id(f"{self._instance}:{file_name}:{self._next()}")
        obj = {
            "file_id": file_id,
            "filename": file_name,
            "notes": notes,
            "object_type": "file",
            "size": len(data),
            "created": int((EPOCH + timedelta(seconds=self._now)).timestamp()),
            "_content_type": content_type,
            "_sha256": hashlib.sha256(data).hexdigest(),
            "_owner": on_behalf_of or PLATFORM,
            "_t": self._now,
        }
        self._mutate()
        self._state.kind("files")[file_id] = obj
        return obj

    # ---- plumbing -----------------------------------------------------------------------------

    def _take(self, name: str | None) -> Fault | None:
        return self._take_full(name)[0]

    def _take_full(self, name: str | None) -> tuple[Fault | None, bool, float | None]:
        if name is None:
            return None, False, None
        queue = self._faults.get(name)
        if not queue:
            return None, False, None
        entry = queue[0]
        entry[1] -= 1
        if entry[1] <= 0:
            queue.pop(0)
        self._taken_extra = entry[4] if len(entry) > 4 else {}
        return entry[0], entry[2], entry[3]

    def _existing(self) -> set[tuple[str, str]]:
        return {(kind, oid) for kind, objs in self._state.objects.items() for oid in objs}

    def _hide_new(self, before: set[tuple[str, str]], lag: float | None) -> None:
        """Hide every object created since `before` until now + lag (default list_lag_s)."""
        delay = self._list_lag_s if lag is None else lag
        if delay <= 0:
            return
        for kind, objs in self._state.objects.items():
            for oid, obj in objs.items():
                if (kind, oid) not in before:
                    obj["_visible_at"] = self._now + delay

    def _visible(self, obj: Mapping[str, Any]) -> bool:
        return self._now >= obj.get("_visible_at", float("-inf"))

    def _corr(self) -> list[dict[str, str]]:
        return _corr_item(self._next())

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _uuid(self, kind: str) -> str:
        seed = f"fake-airwallex:{self._instance}:{kind}:{self._next()}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

    def _acct_id(self) -> str:
        return "acct_" + _b64id(f"{self._instance}:acct:{self._next()}")[:22]

    def _ts(self) -> str:
        return (EPOCH + timedelta(seconds=self._now)).strftime("%Y-%m-%dT%H:%M:%S+0000")

    @staticmethod
    def _public(obj: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in obj.items() if not k.startswith("_")}

    def _owner(self, on_behalf_of: str | None) -> _Account:
        if on_behalf_of is None:
            return self._state.accounts[PLATFORM]
        acct = self._state.accounts.get(on_behalf_of)
        if acct is None or acct.id == PLATFORM:
            raise _err(401, "credentials_invalid", "Access Denied", source="")
        return acct

    def _mutate(self) -> None:
        self._previous = copy.deepcopy(self._state)

    def _dispatch(self, method: str, path: str, body: Any, obo: str | None) -> tuple[int, Any]:
        if not isinstance(body, dict):
            body = {}
        handlers: tuple[tuple[str, Callable[..., tuple[int, Any]]], ...] = (
            ("/api/v1/transfers/create", self._transfer_create),
            ("/api/v1/simulation/transfers/{id}/transition", self._transfer_transition),
            ("/api/v1/beneficiaries/create", self._beneficiary_create),
            ("/api/v1/beneficiaries/validate", self._beneficiary_validate),
            ("/api/v1/fx/quotes/create", self._quote_create),
            ("/api/v1/fx/conversions/create", self._conversion_create),
            ("/api/v1/global_accounts/create", self._global_account_create),
            ("/api/v1/simulation/deposit/create", self._deposit),
            ("/api/v1/accounts/create", self._account_create),
            ("/api/v1/accounts/{id}/update", self._account_update),
            ("/api/v1/accounts/{id}/submit", self._account_submit),
            ("/api/v1/simulation/accounts/{id}/update_status", self._account_status),
            ("/api/v1/connected_account_transfers/create", self._cat_create),
            ("/api/v1/charges/create", self._charge_create),
            ("/api/v1/issuing/cardholders/create", self._cardholder_create),
            ("/api/v1/issuing/cards/create", self._card_create),
            ("/api/v1/issuing/cards/{id}/update", self._card_update),
            ("/api/v1/simulation/issuing/create", self._card_auth),
            (
                "/api/v1/simulation/issuing/card_transaction_lifecycles/{id}/capture",
                self._card_capture,
            ),
            (
                "/api/v1/simulation/issuing/card_transaction_lifecycles/{id}/reverse",
                self._card_reverse,
            ),
            ("/api/v1/simulation/issuing/{id}/capture", self._card_old_capture),
            ("/api/v1/pa/payment_intents/create", self._pi_create),
            ("/api/v1/pa/payment_intents/{id}/confirm", self._pi_confirm),
            ("/api/v1/simulation/pa/payment_disputes/create", self._dispute_create),
            ("/api/v1/pa/payment_disputes/{id}/accept", self._dispute_accept),
            ("/api/v1/pa/payment_disputes/{id}/challenge", self._dispute_challenge),
            ("/api/v1/simulation/pa/payment_disputes/{id}/escalate", self._dispute_escalate),
            ("/api/v1/simulation/pa/payment_disputes/{id}/resolve", self._dispute_resolve),
            ("/api/v1/platform_reports/create", self._report_create),
        )
        for template, handler in handlers:
            ids = _match(template, path)
            if ids is None:
                continue
            try:
                return handler(body, obo, *ids)
            except _Reply as reply:
                return reply.status, reply.body
        return 404, {
            "code": "not_found",
            "message": f"The requested endpoint does not exist [{path}]",
            "details": {},
        }

    def _dedupe(self, owner: _Account, kind: str, rid: Any) -> dict[str, Any] | None:
        if not isinstance(rid, str) or not rid:
            return None
        entry = self._state.request_ids.get((owner.id, kind, rid))
        if entry is None:
            return None
        obj_id, first_used = entry
        if self._now - first_used >= self._dedupe_window_s:
            return None  # past the ~7-day window the request_id is new again (G)
        return self._state.kind(kind).get(obj_id)

    def _remember(
        self, owner: _Account, kind: str, rid: str, obj: dict[str, Any], key: str
    ) -> None:
        obj["_owner"] = owner.id
        obj["_t"] = self._now
        self._state.kind(kind)[obj[key]] = obj
        self._state.request_ids[(owner.id, kind, rid)] = (obj[key], self._now)

    def _need_rid(self, body: Mapping[str, Any], code: str = "field_required") -> str:
        rid = body.get("request_id")
        if not isinstance(rid, str) or not rid:
            raise _err(400, code, "field [request_id] is required", source="request_id")
        return rid

    # ---- transfers ----------------------------------------------------------------------------

    def _transfer_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        rid = body.get("request_id")
        if not isinstance(rid, str) or not rid:
            raise _err(400, "validation_failed", "field [request_id] is required")
        if not 10 <= len(rid) <= 50:
            raise _validation_errors("018", "request_id")
        existing = self._dedupe(owner, "transfers", rid)
        if existing is not None:
            raise _err(
                400,
                "duplicate_request_id",
                f"The request ID {rid} has been used for a transfer with the transfer ID "
                f"{existing['id']} before. Please use a different request ID if you wish to "
                "create a separate transfer.",
                details={"id": existing["id"], "request_id": rid},
                source="request_id",
            )
        if "payment_currency" in body:
            raise _validation_errors("001", "payment_currency")
        ccy = body.get("transfer_currency")
        if not isinstance(ccy, str):
            raise _validation_errors("001", "transfer_currency")
        amount = _dec(body.get("transfer_amount"), "transfer_amount")
        if not _fits(amount, ccy) or amount <= 0:
            raise _validation_errors("011", "transfer_amount")
        method = body.get("transfer_method")
        if method not in ("LOCAL", "SWIFT"):
            raise _validation_errors("001", "transfer_method")
        ben = self._state.kind("beneficiaries").get(str(body.get("beneficiary_id")))
        if ben is None or ben["_owner"] != owner.id:
            raise _validation_errors("001", "beneficiary_id")
        if owner.id != PLATFORM and not owner.has_ein:
            raise _validation_errors("001", "payer")
        source_ccy = body.get("source_currency") or ccy
        fee = SWIFT_FEE.get(ccy, DEFAULT_SWIFT_FEE) if method == "SWIFT" else Decimal(0)
        payer_pays = amount + fee
        if source_ccy != ccy:
            payer_pays = (payer_pays * self._usd_price[ccy] / self._usd_price[source_ccy]).quantize(
                Decimal(1).scaleb(-minor_units(source_ccy)), ROUND_HALF_UP
            )
        bal = owner.bal(source_ccy)
        if bal[0] < payer_pays:
            raise _Reply(*_insufficient("transfers.create", self._corr()))
        self._mutate()
        bal[0] -= payer_pays
        tid = self._uuid("transfer")
        obj = {
            "id": tid,
            "request_id": rid,
            "beneficiary_id": ben["id"],
            "beneficiary": self._public(ben)["beneficiary"],
            "transfer_amount": _num(amount),
            "transfer_currency": ccy,
            "source_amount": _num(payer_pays),
            "source_currency": source_ccy,
            "amount_payer_pays": _num(payer_pays),
            "amount_beneficiary_receives": _num(amount),
            "fee_amount": _num(fee),
            "fee_currency": ccy,
            "fee_paid_by": "PAYER",
            "transfer_method": method,
            "reason": body.get("reason"),
            "reference": body.get("reference"),
            "status": "SCHEDULED",
            "funding": {"status": "FUNDED"},
            "short_reference_id": f"P261006-{self._seq:07d}",
            "created_at": self._ts(),
            "updated_at": self._ts(),
            "_payer_pays": payer_pays,
            "_source_ccy": source_ccy,
        }
        self._remember(owner, "transfers", rid, obj, "id")
        self._post(owner.id, source_ccy, -payer_pays, "PAYOUT", tid)
        return 201, self._public(obj)

    def _transfer_transition(
        self, body: dict[str, Any], obo: str | None, transfer_id: str
    ) -> tuple[int, Any]:
        owner = self._owner(obo)
        t = self._state.kind("transfers").get(transfer_id)
        # A: simulations never act on an object still hidden by a lag (FAKE-FIDELITY).
        if t is None or t["_owner"] != owner.id or not self._visible(t):
            raise _err(
                400,
                "bad_request",
                f"Can't find payment {transfer_id} associated with account "
                f"{PLATFORM_ACCOUNT_UUID if owner.id == PLATFORM else owner.id}",
            )
        nxt = body.get("next_status")
        if "failure_type" in body and body["failure_type"] is None:
            raise _err(
                400,
                "bad_request",
                "Request could not be processed",
                source='Missing body parameter "failure_type"',
            )
        ftype = body.get("failure_type")
        if ftype is not None and ftype not in TRANSFER_FAILURE_TYPES:
            raise _err(
                400,
                "bad_request",
                "Request could not be processed",
                source='Invalid value found for "failure_type"',
            )
        cur = t["status"]
        in_flight = cur in ("SCHEDULED", "PROCESSING")
        allowed = (
            (nxt == "SENT" and in_flight)
            # r1 [r1-cancel-from-sent]: CANCELLED from SENT is a 500 and is not applied.
            or (nxt == "CANCELLED" and in_flight)
            or (nxt in ("PAID", "FAILED") and cur == "SENT")
        )
        if not allowed:
            raise _err(500, "operation_failed", "Request could not be processed")
        self._mutate()
        reply_extra: dict[str, Any] = {}
        if nxt in ("SENT", "PAID"):
            t["status"] = nxt
        else:
            owner.bal(t["_source_ccy"])[0] += t["_payer_pays"]
            self._post(owner.id, t["_source_ccy"], t["_payer_pays"], "PAYOUT_REVERSAL", t["id"])
            t["status"] = "CANCELLED"
            if nxt == "CANCELLED":
                t["failure"] = {"details": {"type": "PAYMENT_RECALLED"}, "message": "simulation"}
                reply_extra = {"failure_reason": "simulation", "failure_type": "PAYMENT_RECALLED"}
            else:
                code, message = FAILURE_CODES.get(
                    ftype, ("90000", (ftype or "OTHER").replace("_", " ").capitalize())
                )
                t["failure"] = {
                    "code": code,
                    "details": {"type": "INCORRECT_ROUTING"},
                    "message": message,
                }
                reply_extra = {
                    "failure_reason": f"{code}: {message}",
                    "failure_type": "INCORRECT_ROUTING",
                }
        t["updated_at"] = self._ts()
        reply = self._public(t)
        if nxt in ("FAILED", "CANCELLED"):
            reply.pop("failure", None)
            reply["status"] = "FAILED"
            reply.update(reply_extra)
        return 200, reply

    # ---- beneficiaries ------------------------------------------------------------------------

    def _check_beneficiary(self, body: dict[str, Any]) -> dict[str, Any]:
        if "payment_methods" in body and "transfer_methods" not in body:
            raise _validation_errors("001", "transfer_methods")
        ben = body.get("beneficiary")
        if not isinstance(ben, dict):
            raise _validation_errors("001", "beneficiary")
        bank = ben.get("bank_details") or {}
        cat = bank.get("bank_account_category")
        if cat is not None and cat not in ("Checking", "Savings"):
            raise _validation_errors("016", "beneficiary.bank_details.bank_account_category")
        if bank.get("account_routing_type1") == "SORT_CODE":
            raise _validation_errors("011", "beneficiary.bank_details.account_routing_type1")
        addr = ben.get("address") or {}
        state = addr.get("state")
        if (
            addr.get("country_code") == "US"
            and isinstance(state, str)
            and not state.startswith("US-")
        ):
            raise _validation_errors("016", "beneficiary.address.state")
        return ben

    def _beneficiary_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        # Sandbox, 6 Oct ([r1-ben-replay-same]): a replayed request_id creates a second
        # beneficiary; there is no deduplication.
        rid = self._need_rid(body, "validation_failed")
        ben = self._check_beneficiary(body)
        self._mutate()
        obj = {
            "id": self._uuid("beneficiary"),
            "request_id": rid,
            "nickname": body.get("nickname"),
            "beneficiary": ben,
            "transfer_methods": body.get("transfer_methods", ["LOCAL"]),
            "payer_entity_type": "COMPANY",
        }
        obj["_owner"], obj["_t"] = owner.id, self._now
        self._state.kind("beneficiaries")[obj["id"]] = obj
        return 201, self._public(obj)

    def _beneficiary_validate(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        self._owner(obo)
        self._check_beneficiary(body)
        return 200, {}

    # ---- FX -----------------------------------------------------------------------------------

    def _fx_ready(self, owner: _Account) -> None:
        if owner.id == PLATFORM:
            return
        if owner.status != "ACTIVE":
            raise _err(403, "forbidden", "Forbidden", details=self._corr())
        if owner.activated_at is None or self._now - owner.activated_at < self._fx_ready_delay_s:
            raise _err(
                400,
                "unconfigured_client_fee",
                "Unable to find relevant client fee in client's pricing schedule",
                details=self._corr(),
            )

    def _rate(self, sell: str, buy: str) -> tuple[str, Decimal, Decimal]:
        """(currency_pair, mid, client rate) where rate = units of pair[3:] per unit of pair[:3]."""
        if sell not in self._usd_price or buy not in self._usd_price:
            raise _err(
                400,
                "validation_error",
                "unsupported_currency",
                details=self._corr(),
                source="fx_resolver",
            )
        base, counter = sorted((sell, buy), key=lambda c: -self._usd_price[c])
        mid = self._usd_price[base] / self._usd_price[counter]
        # The client always gets the worse side of the pair.
        client = mid * (1 + CLIENT_MARKUP) if buy == base else mid * (1 - CLIENT_MARKUP)
        return (
            base + counter,
            mid.quantize(Decimal("0.000001")),
            client.quantize(Decimal("0.000001")),
        )

    def _price(self, body: dict[str, Any], client: Decimal, pair: str) -> tuple[Decimal, Decimal]:
        sell, buy = body["sell_currency"], body["buy_currency"]
        base = pair[:3]
        buy_amount, sell_amount = body.get("buy_amount"), body.get("sell_amount")
        if (buy_amount is None) == (sell_amount is None):
            raise _err(
                400,
                "validation_error",
                "exactly one of buy_amount or sell_amount",
                details=self._corr(),
                source="buy_amount",
            )
        # sell units per buy unit
        per_buy = client if buy == base else 1 / client
        if buy_amount is not None:
            b = _dec(buy_amount, "buy_amount")
            s = (b * per_buy).quantize(Decimal(1).scaleb(-minor_units(sell)), ROUND_HALF_UP)
        else:
            s = _dec(sell_amount, "sell_amount")
            b = (s / per_buy).quantize(Decimal(1).scaleb(-minor_units(buy)), ROUND_DOWN)
        return b, s

    def _quote_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        for f in ("buy_currency", "sell_currency"):
            if not isinstance(body.get(f), str):
                raise _err(400, "field_required", f"field [{f}] is required", source=f)
        validity = body.get("validity")
        if validity is None:
            raise _err(400, "field_required", "field [validity] is required", source="validity")
        durations = {
            "MIN_1": 60,
            "MIN_15": 900,
            "MIN_30": 1800,
            "HR_1": 3600,
            "HR_4": 14400,
            "HR_8": 28800,
            "HR_24": 86400,
        }
        if validity not in durations:
            raise _err(
                400, "invalid_argument", f"{validity} is not a valid type", source="validity"
            )
        self._fx_ready(owner)
        pair, mid, client = self._rate(body["sell_currency"], body["buy_currency"])
        b, s = self._price(body, client, pair)
        self._mutate()
        qid = self._uuid("quote")
        obj = {
            "quote_id": qid,
            "buy_currency": body["buy_currency"],
            "sell_currency": body["sell_currency"],
            "buy_amount": _num(b),
            "sell_amount": _num(s),
            "currency_pair": pair,
            "dealt_currency": body["buy_currency"]
            if "buy_amount" in body
            else body["sell_currency"],
            "client_rate": client,
            "mid_rate": mid,
            "awx_rate": mid,
            "conversion_date": self._ts()[:10],
            "valid_from_at": self._ts(),
            "valid_to_at": (EPOCH + timedelta(seconds=self._now + durations[validity])).strftime(
                "%Y-%m-%dT%H:%M:%S+0000"
            ),
            "validity": validity,
            "usage": "MULTI_USE",
            "_expires": self._now + durations[validity],
            "_owner": owner.id,
            "_client": client,
        }
        self._state.kind("quotes")[qid] = obj
        return 200, self._public(obj)

    def _conversion_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        rid = self._need_rid(body)
        if self._dedupe(owner, "conversions", rid) is not None:
            raise _err(
                400,
                "invalid_argument",
                f"request_id '{rid}' has been used in the past. Please use a different "
                "request_id for this request",
                details=self._corr(),
                source="request_id",
            )
        for f in ("buy_currency", "sell_currency"):
            if not isinstance(body.get(f), str):
                raise _err(400, "field_required", f"field [{f}] is required", source=f)
        self._fx_ready(owner)
        pair, _mid, client = self._rate(body["sell_currency"], body["buy_currency"])
        qid = body.get("quote_id")
        if qid is not None:
            q = self._state.kind("quotes").get(qid)
            if q is None or q["_owner"] != owner.id:
                raise _err(
                    400,
                    "invalid_argument",
                    "quote not found",
                    details=self._corr(),
                    source="quote_id",
                )
            if (q["buy_currency"], q["sell_currency"]) != (
                body["buy_currency"],
                body["sell_currency"],
            ):
                raise _err(
                    400,
                    "conflict_original_ccy",
                    f"Quote currencies (buy={q['buy_currency']}, sell={q['sell_currency']}) differ "
                    f"from currencies in execution request (buy={body['buy_currency']}, "
                    f"sell={body['sell_currency']})",
                    details=self._corr(),
                )
            if self._now >= q["_expires"]:
                raise _err(400, "expired_quote", "Quote has expired", details=self._corr())
            client = q["_client"]
        b, s = self._price(body, client, pair)
        if s * self._usd_price[body["sell_currency"]] > FX_LIMIT_USD:
            raise _err(
                400,
                "validation_error",
                "amount_above_limit",
                details=self._corr(),
                source="fx_resolver",
            )
        sell_bal = owner.bal(body["sell_currency"])
        if sell_bal[0] < s:
            raise _Reply(*_insufficient("fx.conversions.create", self._corr()))
        self._mutate()
        sell_bal[0] -= s
        owner.bal(body["buy_currency"])[0] += b
        cid = self._uuid("conversion")
        obj = {
            "conversion_id": cid,
            "request_id": rid,
            "buy_currency": body["buy_currency"],
            "sell_currency": body["sell_currency"],
            "buy_amount": _num(b),
            "sell_amount": _num(s),
            "client_rate": client,
            "currency_pair": pair,
            "dealt_currency": body["buy_currency"]
            if "buy_amount" in body
            else body["sell_currency"],
            "quote_id": qid,
            "status": "SETTLED",
            "conversion_date": self._ts()[:10],
            "created_at": self._ts(),
            "updated_at": self._ts(),
        }
        if qid is None:
            obj.pop("quote_id")
        self._remember(owner, "conversions", rid, obj, "conversion_id")
        self._post(owner.id, body["sell_currency"], -s, "CONVERSION_SELL", cid)
        self._post(owner.id, body["buy_currency"], b, "CONVERSION_BUY", cid)
        return 201, self._public(obj)

    # ---- global accounts, deposits ------------------------------------------------------------

    def _global_account_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        rid = self._need_rid(body)
        existing = self._dedupe(owner, "global_accounts", rid)
        if existing is not None:
            raise _err(
                400,
                "duplicate_request_id",
                f"The request ID {rid} has been used before.",
                details={"id": existing["id"]},
                source="request_id",
            )
        features = body.get("required_features")
        if not isinstance(features, list) or not features:
            raise _err(
                400,
                "field_required",
                "field [required_features] is required",
                source="required_features",
            )
        self._mutate()
        obj = {
            "id": self._uuid("global_account"),
            "request_id": rid,
            "country_code": body.get("country_code"),
            "required_features": features,
            "status": "ACTIVE",
            "account_name": owner.nickname,
            "_currency": features[0].get("currency"),
        }
        self._remember(owner, "global_accounts", rid, obj, "id")
        return 201, self._public(obj)

    def _deposit(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        ga = self._state.kind("global_accounts").get(str(body.get("global_account_id")))
        if ga is None or ga["_owner"] != owner.id or not self._visible(ga):
            raise _err(
                400, "bad_request", "Request could not be processed", source="global_account_id"
            )
        amount = _dec(body.get("amount"), "amount")
        self._mutate()
        owner.bal(ga["_currency"])[0] += amount  # posts immediately although the reply says PENDING
        obj = {
            "id": self._uuid("deposit"),
            "amount": _num(amount),
            "currency": ga["_currency"],
            "payer_name": body.get("payer_name"),
            "status": "PENDING",
            "deposit_type": "Credit",
            "create_time": self._ts(),
            "_owner": owner.id,
            "_t": self._now,
        }
        self._state.kind("deposits")[obj["id"]] = obj
        self._post(owner.id, ga["_currency"], amount, "DEPOSIT", obj["id"])
        return 201, self._public(obj)

    # ---- connected accounts -------------------------------------------------------------------

    def _account_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        if obo is not None:
            raise _err(401, "credentials_invalid", "Access Denied", source="")
        details = body.get("account_details")
        if not isinstance(details, dict):
            raise _err(
                400,
                "invalid_argument",
                "account_details is not of the expected type; "
                "please refer to our API documentation",
                source="account_details",
            )
        ids = (details.get("business_details") or {}).get("business_identifiers") or []
        self._mutate()
        acct = _Account(
            self._acct_id(),
            str(body.get("nickname") or ""),
            "CREATED",
            details,
            has_ein=any(isinstance(i, dict) and i.get("type") == "EIN" for i in ids),
        )
        self._state.accounts[acct.id] = acct
        return 201, self._account_view(acct, body)

    def _account_view(
        self, acct: _Account, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        view = {
            "id": acct.id,
            "status": acct.status,
            "nickname": acct.nickname,
            "account_details": acct.details,
        }
        if body:
            for k in ("identifier", "metadata", "primary_contact"):
                if k in body:
                    view[k] = body[k]
        return view

    def _connected(self, account_id: str) -> _Account:
        acct = self._state.accounts.get(account_id)
        if acct is None or acct.id == PLATFORM:
            raise _err(404, "not_found", "Account not found")
        return acct

    def _account_update(
        self, body: dict[str, Any], obo: str | None, account_id: str
    ) -> tuple[int, Any]:
        acct = self._connected(account_id)
        if acct.status != "CREATED":
            raise _err(400, "invalid_argument", "account can no longer be updated")
        self._mutate()
        details = body.get("account_details") or {}
        for k, v in details.items():
            if k == "business_person_details":
                continue  # the guide: a business person added on update does not persist
            acct.details[k] = v
        return 200, self._account_view(acct)

    def _account_submit(
        self, body: dict[str, Any], obo: str | None, account_id: str
    ) -> tuple[int, Any]:
        acct = self._connected(account_id)
        if acct.status != "CREATED":
            raise _err(400, "invalid_argument", "account is already submitted")
        self._mutate()
        acct.status = "SUBMITTED"
        return 200, self._account_view(acct)

    def _account_status(
        self, body: dict[str, Any], obo: str | None, account_id: str
    ) -> tuple[int, Any]:
        acct = self._connected(account_id)
        nxt = body.get("next_status")
        if nxt not in ("ACTIVE", "SUSPENDED", "ACTION_REQUIRED", "CLOSED"):
            raise _err(400, "invalid_argument", "next_status is invalid", source="next_status")
        self._mutate()
        acct.status = nxt
        if nxt == "ACTIVE":
            acct.activated_at = self._now
        return 200, self._account_view(acct)

    # ---- platform money movement --------------------------------------------------------------

    def _platform_move(self, body: dict[str, Any], obo: str | None, kind: str) -> tuple[int, Any]:
        is_charge = kind == "charges"
        field_name = "source" if is_charge else "destination"
        rid = self._need_rid(body)
        if not isinstance(body.get("reference"), str) or not body["reference"]:
            raise _err(400, "field_required", "field [reference] is required", source="reference")
        reason = body.get("reason")
        if reason not in PLATFORM_REASONS:
            listing = sorted(PLATFORM_REASONS | FIRST_VALIDATOR_EXTRA)
            if reason in FIRST_VALIDATOR_EXTRA:
                raise _err(
                    400,
                    "validation_failed",
                    f"Reason must be one of [{', '.join(sorted(PLATFORM_REASONS))}]",
                )
            raise _err(
                400,
                "invalid_argument",
                f"Reason must be one of [{', '.join(listing)}]",
                source="reason",
            )
        if obo is not None:
            if is_charge:
                raise _err(400, "validation_failed", "Permission denied.", source="")
            # Sandbox: the header makes that connected account the SOURCE of the transfer.
            payer = self._owner(obo)
        else:
            payer = self._state.accounts[PLATFORM]
        account_id = body.get(field_name)
        target = self._state.accounts.get(account_id) if isinstance(account_id, str) else None
        if target is None or target.id == PLATFORM:
            raise _err(400, "invalid_argument", f"{field_name} is invalid", source=field_name)
        src, dst = (target, self._state.accounts[PLATFORM]) if is_charge else (payer, target)
        if src.id == dst.id:
            raise _err(
                400,
                "validation_failed",
                "Not allowed to do transfer when source and destination account are the same.",
            )
        owner_key = self._state.accounts[PLATFORM]
        existing = self._dedupe(owner_key, kind, rid)
        if existing is not None:
            raise _err(
                400,
                "request_id_duplicate",
                f"request_id '{rid}' has been used in the past.  please use a different "
                "request_id for this request",
                details={
                    "source_id": existing["id"],
                    "source_type": "charge_id" if is_charge else "transfer_id",
                },
                source="request_id",
            )
        ccy = body.get("currency")
        if not isinstance(ccy, str):
            raise _err(400, "field_required", "field [currency] is required", source="currency")
        amount = _dec(body.get("amount"), "amount")
        if amount <= 0 or not _fits(amount, ccy):
            raise _err(400, "invalid_argument", "amount is invalid", source="amount")
        if src.bal(ccy)[0] < amount:
            raise _Reply(
                *_insufficient(
                    "charges.create" if is_charge else "connected_account_transfers.create",
                    self._corr(),
                )
            )
        self._mutate()
        src.bal(ccy)[0] -= amount
        dst.bal(ccy)[0] += amount
        obj = {
            "id": ("ch_" if is_charge else "tr_")
            + _b64id(f"{self._instance}:{kind}:{self._next()}")[:22],
            "request_id": rid,
            "amount": _num(amount),
            "currency": ccy,
            field_name: account_id,
            "fee": 0,
            "reason": reason,
            "reference": body["reference"],
            "status": "NEW",
            "short_reference_id": f"D261006-{self._seq:07d}",
            "created_at": self._ts(),
            "updated_at": self._ts(),
            "_src": src.id,
            "_dst": dst.id,
        }
        if body.get("_end_as"):
            obj["_end_as"] = body["_end_as"]
        self._remember(owner_key, kind, rid, obj, "id")
        label = "CHARGE" if is_charge else "TRANSFER"
        self._post(src.id, ccy, -amount, f"{label}_OUT", obj["id"])
        self._post(dst.id, ccy, amount, f"{label}_IN", obj["id"])
        return 201, self._public(obj)

    def _cat_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        return self._platform_move(body, obo, "connected_account_transfers")

    def _charge_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        return self._platform_move(body, obo, "charges")

    # ---- issuing ------------------------------------------------------------------------------

    def _cardholder_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        email = body.get("email")
        if not isinstance(email, str) or not email:
            raise _err(400, "invalid_argument", "email is mandatory", source="email")
        ctype = body.get("type", "INDIVIDUAL")
        for ch in self._state.kind("cardholders").values():
            if ch["_owner"] == owner.id and ch["email"] == email:
                raise _err(
                    400,
                    "bad_request",
                    "The given email or mobile number is already associated with a cardholder.",
                )
        if ctype == "INDIVIDUAL" and not isinstance(body.get("individual"), dict):
            raise _err(400, "invalid_argument", "individual is mandatory", source="individual")
        self._mutate()
        obj = {
            "cardholder_id": self._uuid("cardholder"),
            "email": email,
            "type": ctype,
            "status": "READY" if ctype == "DELEGATE" else "PENDING",
            "_owner": owner.id,
            "_t": self._now,
        }
        if ctype == "INDIVIDUAL":
            obj["individual"] = body["individual"]
        self._state.kind("cardholders")[obj["cardholder_id"]] = obj
        return 202, self._public(obj)

    def _card_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        rid = self._need_rid(body, "invalid_argument")
        if self._dedupe(owner, "cards", rid) is not None:
            raise _err(
                400,
                "invalid_argument",
                "The provided ID is already associated with a previous request",
            )
        holder = self._state.kind("cardholders").get(str(body.get("cardholder_id")))
        if holder is None or holder["_owner"] != owner.id:
            raise _err(400, "invalid_argument", "cardholder not found", source="cardholder_id")
        if not isinstance(body.get("program"), dict):
            raise _err(400, "invalid_argument", "program is mandatory", source="program")
        controls = body.get("authorization_controls")
        if not isinstance(controls, dict):
            raise _err(
                400,
                "invalid_argument",
                "authorization_controls is mandatory",
                source="authorization_controls",
            )
        limits = (controls.get("transaction_limits") or {}).get("limits") or []
        for lim in limits:
            if lim.get("interval") not in LIMIT_INTERVALS or _dec(lim.get("amount"), "amount") <= 0:
                raise _err(
                    400,
                    "invalid_argument",
                    "transaction limit is invalid",
                    source="authorization_controls.transaction_limits",
                )
        self._mutate()
        obj = {
            "card_id": self._uuid("card"),
            "request_id": rid,
            "cardholder_id": holder["cardholder_id"],
            "card_status": "ACTIVE",
            "form_factor": body.get("form_factor"),
            "is_personalized": body.get("is_personalized"),
            "nick_name": body.get("nick_name"),
            "metadata": body.get("metadata"),
            "program": {"type": "DEBIT", **body["program"]},
            "authorization_controls": controls,
            "created_by": body.get("created_by"),
            "card_number": "************" + f"{self._seq:04d}"[-4:],
            "brand": "VISA",
            "created_at": self._ts(),
        }
        self._remember(owner, "cards", rid, obj, "card_id")
        return 202, self._public(obj)

    def _card_update(self, body: dict[str, Any], obo: str | None, card_id: str) -> tuple[int, Any]:
        owner = self._owner(obo)
        card = self._state.kind("cards").get(card_id)
        if card is None or card["_owner"] != owner.id:
            raise _err(404, "NOT_FOUND", "NOT_FOUND", source="")
        status = body.get("card_status")
        if status is not None and status not in ("ACTIVE", "INACTIVE", "CLOSED"):
            raise _err(400, "invalid_argument", "card_status is invalid", source="card_status")
        if card["card_status"] == "CLOSED":
            raise _err(400, "invalid_argument", "card is closed", source="card_status")
        self._mutate()
        if status is not None:
            card["card_status"] = status
        if isinstance(body.get("authorization_controls"), dict):
            card["authorization_controls"] = {
                **card["authorization_controls"],
                **body["authorization_controls"],
            }
        return 200, self._public(card)

    def _spent(self, card_id: str, interval: str, limit_ccy: str = "USD") -> Decimal:
        start = {"ALL_TIME": -1e18, "PER_TRANSACTION": self._now}.get(interval)
        if start is None:
            span = {"DAILY": 86400, "WEEKLY": 7 * 86400, "MONTHLY": 30 * 86400}[interval]
            start = self._now - span  # AWS-authored: a rolling window, not a calendar one
        total = Decimal(0)
        for txn in self._state.kind("card_transactions").values():
            if (
                txn["card_id"] == card_id
                and txn["status"] in ("PENDING", "APPROVED")
                and txn["_t"] >= start
                and txn["transaction_type"] != "REVERSAL"
            ):
                amount = txn["_amount"]
                if txn["_ccy"] != limit_ccy:
                    amount = amount * self._usd_price[txn["_ccy"]] / self._usd_price[limit_ccy]
                total += amount
        return total

    def _decide(
        self,
        card: dict[str, Any],
        owner: _Account,
        amount: Decimal,
        ccy: str,
        mcc: str | None,
        forced: str | None,
    ) -> str | None:
        if card["card_status"] == "INACTIVE":
            return "CARD_INACTIVE"
        if card["card_status"] == "CLOSED":
            return "CARD_CLOSED"
        if forced and forced != "_NSF":
            return forced  # transaction_failure_reason: an unconditional override
        ac = card["authorization_controls"]
        if ac.get("allowed_currencies") and ccy not in ac["allowed_currencies"]:
            return "CURRENCY_NOT_ALLOWED"
        if ac.get("allowed_merchant_categories") and mcc not in ac["allowed_merchant_categories"]:
            return "MERCHANT_CATEGORY_NOT_ALLOWED"
        tl = ac.get("transaction_limits") or {}
        lim_ccy = tl.get("currency", "USD")
        in_lim = (
            amount if lim_ccy == ccy else amount * self._usd_price[ccy] / self._usd_price[lim_ccy]
        )
        for lim in tl.get("limits") or []:
            cap = _dec(lim["amount"], "amount")
            if lim["interval"] == "PER_TRANSACTION":
                if in_lim > cap:  # inclusive: amount equal to the limit clears
                    return "LIMIT_EXCEEDED"
            elif self._spent(card["card_id"], lim["interval"], lim_ccy) + in_lim > cap:
                return "LIMIT_EXCEEDED"
        # Observed: limits, currency and MCC come before funds; an injected NSF fault too.
        if forced == "_NSF" or owner.bal(ccy)[0] < amount:
            return "INSUFFICIENT_FUNDS"
        return None

    def _card_auth(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        caller = self._owner(obo)
        card = self._state.kind("cards").get(str(body.get("card_id")))
        if card is None or not self._visible(card):
            raise _err(400, "invalid_argument", "card not found", source="card_id")
        if "transaction_amount" not in body:
            raise _err(
                400,
                "bad_request",
                "Request could not be processed",
                source='Missing body parameter "transaction_amount"',
            )
        amount = _dec(body["transaction_amount"], "transaction_amount")
        ccy = body.get("transaction_currency")
        if ccy not in CARD_SIM_CURRENCIES:
            raise _err(
                400,
                "invalid_argument",
                "transaction_currency is invalid",
                source="transaction_currency",
            )
        owner = self._state.accounts[card["_owner"]]
        if caller.id not in (owner.id, PLATFORM):
            # Another connected account's card: refuse with no write. (A: the sandbox evidence
            # covers only the no-header case, below.)
            raise _err(400, "invalid_argument", "card not found", source="card_id")
        mcc = body.get("merchant_category_code")
        forced = "_NSF" if body.get("_forced_failure") else body.get("transaction_failure_reason")
        reason = self._decide(card, owner, amount, ccy, mcc, forced)
        single = bool(body.get("single_phase"))
        self._mutate()
        lifecycle = self._uuid("lifecycle")
        txn_id = self._uuid("card_transaction")
        if reason is None:
            if single:
                owner.bal(ccy)[0] -= amount
            else:
                owner.bal(ccy)[0] -= amount
                owner.bal(ccy)[1] += amount
            kind = "CARD_PURCHASE" if single else "CARD_AUTHORIZATION"
            self._post(owner.id, ccy, -amount, kind, lifecycle)
        # Sandbox, 6 Oct ([r1-single-phase-0.02]): a declined single-phase call is also CLEARING.
        ttype = "CLEARING" if single else "AUTHORIZATION"
        status = "FAILED" if reason else ("APPROVED" if single else "PENDING")
        listed = {
            "transaction_id": self._uuid("issuing_transaction"),
            "card_id": card["card_id"],
            "transaction_type": ttype,
            "status": status,
            "failure_reason": reason,
            "transaction_amount": _num(-amount),
            "transaction_currency": ccy,
            "billing_amount": _num(-amount),
            "billing_currency": ccy,
            "merchant": {"category_code": mcc, "name": str(body.get("merchant_info", ""))},
            "transaction_date": self._ts(),
            "card_transaction_data": {
                "card_transaction_id": txn_id,
                "card_transaction_lifecycle_id": lifecycle,
            },
            "_owner": owner.id,
            "_t": self._now,
            "_amount": amount,
            "_ccy": ccy,
            "_lifecycle": lifecycle,
        }
        self._state.kind("card_transactions")[listed["transaction_id"]] = listed
        reply = {
            "card_id": card["card_id"],
            "card_transaction_id": txn_id,
            "card_transaction_lifecycle_id": lifecycle,
            "process_result": "DECLINED" if reason else "APPROVED",
            "type": ttype,
            "subtype": ttype,
            "transaction_amount": _num(amount),
            "transaction_currency": ccy,
        }
        if reason:
            reply["failure_reason"] = reason
        if caller.id != owner.id:
            # Sandbox: without x-on-behalf-of for a connected-account card the call returns 400,
            # yet the authorisation is created.
            raise _err(400, "bad_request", "Transaction could not be created")
        return 201, reply

    def _lifecycle(self, lifecycle_id: str) -> dict[str, Any]:
        for txn in self._state.kind("card_transactions").values():
            if txn["_lifecycle"] == lifecycle_id and self._visible(txn):
                return txn
        raise _err(404, "not_found", "lifecycle not found")

    def _card_capture(
        self, body: dict[str, Any], obo: str | None, lifecycle_id: str
    ) -> tuple[int, Any]:
        if not self._admin_key:
            raise _err(401, "unauthorized", "Admin permissions required")
        txn = self._lifecycle(lifecycle_id)
        if txn["status"] != "PENDING":
            raise _err(400, "invalid_argument", "transaction cannot be captured")
        self._mutate()
        owner = self._state.accounts[txn["_owner"]]
        owner.bal(txn["_ccy"])[1] -= txn["_amount"]
        txn["transaction_type"], txn["status"] = "CLEARING", "APPROVED"
        return 201, {
            "process_result": "APPROVED",
            "type": "CLEARING",
            "subtype": "CLEARING",
            "card_transaction_lifecycle_id": lifecycle_id,
        }

    def _card_reverse(
        self, body: dict[str, Any], obo: str | None, lifecycle_id: str
    ) -> tuple[int, Any]:
        if not self._admin_key:
            raise _err(401, "unauthorized", "Admin permissions required")
        txn = self._lifecycle(lifecycle_id)
        if txn["status"] != "PENDING":
            raise _err(400, "invalid_argument", "transaction cannot be reversed")
        self._mutate()
        owner = self._state.accounts[txn["_owner"]]
        owner.bal(txn["_ccy"])[1] -= txn["_amount"]
        owner.bal(txn["_ccy"])[0] += txn["_amount"]
        self._post(owner.id, txn["_ccy"], txn["_amount"], "CARD_REVERSAL", lifecycle_id)
        txn["transaction_type"], txn["status"] = "REVERSAL", "APPROVED"
        return 201, {
            "process_result": "APPROVED",
            "type": "REVERSAL_AUTH",
            "subtype": "REVERSAL",
            "card_transaction_lifecycle_id": lifecycle_id,
        }

    def _card_old_capture(self, body: dict[str, Any], obo: str | None, _id: str) -> tuple[int, Any]:
        raise _err(
            400, "bad_request", "Use the card_transaction_lifecycles path for this API version"
        )

    # ---- payments and disputes ----------------------------------------------------------------

    def _pi_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        owner = self._owner(obo)
        missing = [
            f
            for f in ("request_id", "currency", "amount", "merchant_order_id")
            if body.get(f) in (None, "")
        ]
        if missing:
            raise _err(
                400,
                "validation_error",
                "; ".join(f"{f} must be provided" for f in missing),
                source="; ".join(missing),
            )
        rid = body["request_id"]
        if self._dedupe(owner, "payment_intents", rid) is not None:
            raise _err(400, "duplicate_request", f"The request ID {rid} has been used previously.")
        self._mutate()
        obj = {
            "id": "int_" + _b64id(f"{self._instance}:pi:{self._next()}")[:19].lower(),
            "request_id": rid,
            "amount": _num(_dec(body["amount"], "amount")),
            "currency": body["currency"],
            "merchant_order_id": body["merchant_order_id"],
            "status": "REQUIRES_PAYMENT_METHOD",
            "metadata": body.get("metadata"),
            "created_at": self._ts(),
        }
        self._remember(owner, "payment_intents", rid, obj, "id")
        return 201, self._public(obj)

    def _pi_confirm(self, body: dict[str, Any], obo: str | None, pi_id: str) -> tuple[int, Any]:
        owner = self._owner(obo)
        pi = self._state.kind("payment_intents").get(pi_id)
        if not self._online_payments:
            raise _err(
                400, "validation_error", "This account is not enabled for native API access."
            )
        if pi is None or pi["_owner"] != owner.id:
            raise _err(404, "not_found", "payment intent not found")
        card = ((body.get("payment_method") or {}).get("card") or {}).get("number")
        self._mutate()
        if card == TEST_CARD:
            pi["status"] = "SUCCEEDED"
            pi["captured_amount"] = pi["amount"]
        else:
            pi["status"] = "REQUIRES_PAYMENT_METHOD"
        return 200, self._public(pi)

    def _dispute(self, dispute_id: str, obo: str | None = None) -> dict[str, Any]:
        d = self._state.kind("disputes").get(dispute_id)
        owner = self._owner(obo).id
        if d is None or (owner != PLATFORM and d["_owner"] != owner) or not self._visible(d):
            raise _err(
                404,
                "not_found",
                f"The requested endpoint does not exist [/api/v1/pa/payment_disputes/{dispute_id}]",
                details={},
            )
        return d

    def _dispute_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        for f in ("payment_intent_id", "due_at"):
            if not body.get(f):
                raise _err(
                    400,
                    "bad_request",
                    "Request could not be processed",
                    source=f'Missing body parameter "{f}"',
                )
        pi = self._state.kind("payment_intents").get(body["payment_intent_id"])
        if pi is None or pi["status"] != "SUCCEEDED" or not self._visible(pi):
            raise _err(500, "operation_failed", "Request could not be processed")
        stage = body.get("stage", "RFI")
        if stage not in ("RFI", "PRE_CHARGEBACK", "CHARGEBACK"):
            raise _err(400, "validation_error", "stage is invalid", source="stage")
        if str(body.get("reason_code")) not in DISPUTE_REASON_CODES:
            raise _err(400, "validation_error", "reason_code is invalid", source="reason_code")
        self._mutate()
        obj = {
            "id": "dst_" + _b64id(f"{self._instance}:dispute:{self._next()}")[:13].lower(),
            "payment_intent_id": pi["id"],
            "amount": pi["amount"],
            "currency": pi["currency"],
            "stage": stage,
            "status": "ACCEPTED" if stage == "PRE_CHARGEBACK" else "REQUIRES_RESPONSE",
            "reason": {"original_code": str(body["reason_code"])},
            "due_at": body["due_at"],
            "accept_details": [],
            "challenge_details": [],
            "created_at": self._ts(),
            "updated_at": self._ts(),
            "_owner": pi["_owner"],
            "_t": self._now,
        }
        self._state.kind("disputes")[obj["id"]] = obj
        return 201, self._public(obj)

    def _dispute_accept(
        self, body: dict[str, Any], obo: str | None, dispute_id: str
    ) -> tuple[int, Any]:
        d = self._dispute(dispute_id, obo)
        self._need_dispute_rid(body)
        reason = body.get("reason", "OTHERS")
        if reason not in ACCEPT_REASONS:
            raise _err(400, "validation_error", "reason is invalid", source="reason")
        if d["status"] != "REQUIRES_RESPONSE":
            raise _err(400, "validation_error", "Dispute transition is not supported")
        self._mutate()
        d["status"] = "ACCEPTED"
        if d["stage"] == "RFI":  # API reference: accepting at RFI triggers a full refund (G)
            pi = self._state.kind("payment_intents").get(d["payment_intent_id"], {})
            refund = {
                "id": "rfd_" + _b64id(f"{self._instance}:refund:{self._next()}")[:16].lower(),
                "payment_intent_id": d["payment_intent_id"],
                "amount": d["amount"],
                "currency": d["currency"],
                "reason": (body.get("refund") or {}).get("reason", "OTHERS"),
                "status": "SUCCEEDED",
                "created_at": self._ts(),
                "_owner": pi.get("_owner", d["_owner"]),
                "_t": self._now,
            }
            self._state.kind("refunds")[refund["id"]] = refund
        d["accept_details"].append(
            {"stage": d["stage"], "reason": reason, "accepted_at": self._ts()}
        )
        d["updated_at"] = self._ts()
        return 200, self._public(d)

    def _dispute_challenge(
        self, body: dict[str, Any], obo: str | None, dispute_id: str
    ) -> tuple[int, Any]:
        d = self._dispute(dispute_id, obo)
        self._need_dispute_rid(body)
        if d["status"] != "REQUIRES_RESPONSE":
            raise _err(400, "validation_error", "Dispute transition is not supported")
        if d["stage"] == "CHARGEBACK" and not body.get("product_type"):
            raise _err(  # API reference: product_type is required at CHARGEBACK (G)
                400, "validation_error", "product_type must be provided", source="product_type"
            )
        reason = body.get("reason")
        if reason is not None and reason not in CHALLENGE_REASONS:
            raise _err(400, "validation_error", "reason is invalid", source="reason")
        docs = ((body.get("supporting_documents") or {}).get("documents")) or []
        file_ids = [fid for doc in docs for fid in (doc.get("file_ids") or [])]
        if not file_ids:
            raise _err(
                400,
                "validation_error",
                "at least one supporting document is required",
                source="supporting_documents",
            )
        files = self._state.kind("files")
        for fid in file_ids:
            f = files.get(fid)
            if f is None:
                raise _err(
                    400, "validation_error", f"file {fid} not found", source="supporting_documents"
                )
            if f["_content_type"] not in ("image/jpeg", "application/pdf"):
                # Guide: PNG is rejected for evidence (the sandbox accepted it at upload).
                raise _err(
                    400, "validation_error", "Unsupported file type", source="supporting_documents"
                )
        self._mutate()
        d["status"] = "CHALLENGED"
        d["challenge_details"].append(
            {
                "stage": d["stage"],
                "reason": reason,
                "supporting_documents": {"documents": docs},
                "challenged_at": self._ts(),
            }
        )
        d["updated_at"] = self._ts()
        return 200, self._public(d)

    @staticmethod
    def _need_dispute_rid(body: Mapping[str, Any]) -> None:
        if not isinstance(body.get("request_id"), str) or not body["request_id"]:
            raise _err(  # API reference: request_id is required on accept and challenge (G)
                400, "validation_error", "request_id must be provided", source="request_id"
            )

    def _page(self, rows: list[dict[str, Any]], query: Mapping[str, str]) -> dict[str, Any]:
        try:
            size, num = int(query.get("page_size", "100")), int(query.get("page_num", "0"))
        except ValueError:
            raise _err(400, "invalid_argument", "page parameters must be integers") from None
        if size < 10 or num < 0:
            raise _err(400, "invalid_argument", "BELOW_MIN_PAGE_SIZE", source="")
        page = rows[num * size : (num + 1) * size]
        return {"items": page, "has_more": len(rows) > (num + 1) * size}

    def _window(self, query: Mapping[str, str], lo: str, hi: str) -> tuple[float, float]:
        def at(key: str, default: float) -> float:
            value = query.get(key)
            if not value:
                return default
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise _err(400, "invalid_argument", f"{key} is not a date-time") from None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return (parsed - EPOCH).total_seconds()

        return at(lo, float("-inf")), at(hi, float("inf"))

    def _financial_transactions(self, query: Mapping[str, str], owner: _Account) -> dict[str, Any]:
        """A: deposits only; the shape is assumed until a live read records the real one."""
        start, end = self._window(query, "from_created_at", "to_created_at")
        rows = [
            {
                "id": e["id"],
                "transaction_type": "DEPOSIT",
                "currency": e["currency"],
                "amount": _num(e["amount"]),
                "source_id": e["source_id"],
                "status": "SETTLED",
                "created_at": (EPOCH + timedelta(seconds=e["t"])).strftime(
                    "%Y-%m-%dT%H:%M:%S+0000"
                ),
            }
            for e in self._ledger
            if e["account"] == owner.id
            and e["transaction_type"] == "DEPOSIT"
            and start <= e["t"] < end
            and query.get("currency") in (None, e["currency"])
        ]
        return self._page(rows, query)

    def _balance_history(self, query: Mapping[str, str], owner: _Account) -> dict[str, Any]:
        """A: every balance change of the account, oldest first, with the running balance."""
        start, end = self._window(query, "from_post_at", "to_post_at")
        running: dict[str, Decimal] = {}
        rows = []
        for e in self._ledger:
            if e["account"] != owner.id:
                continue
            running[e["currency"]] = running.get(e["currency"], Decimal(0)) + e["amount"]
            if not start <= e["t"] < end or query.get("currency") not in (None, e["currency"]):
                continue
            rows.append(
                {
                    "id": e["id"],
                    "transaction_type": e["transaction_type"],
                    "currency": e["currency"],
                    "amount": _num(e["amount"]),
                    "balance": _num(running[e["currency"]]),
                    "source_id": e["source_id"],
                    "posted_at": (EPOCH + timedelta(seconds=e["t"])).strftime(
                        "%Y-%m-%dT%H:%M:%S+0000"
                    ),
                }
            )
        return self._page(rows, query)

    def _dispute_escalate(
        self, body: dict[str, Any], obo: str | None, dispute_id: str
    ) -> tuple[int, Any]:
        d = self._dispute(dispute_id, obo)
        if d["stage"] != "RFI":
            raise _err(400, "validation_error", "Dispute transition is not supported")
        self._mutate()
        d["stage"], d["status"] = "CHARGEBACK", "REQUIRES_RESPONSE"
        d["updated_at"] = self._ts()
        return 200, self._public(d)

    def _dispute_resolve(
        self, body: dict[str, Any], obo: str | None, dispute_id: str
    ) -> tuple[int, Any]:
        d = self._dispute(dispute_id, obo)
        outcome = body.get("outcome", body.get("status"))
        if outcome not in ("WON", "LOST"):
            raise _err(400, "validation_error", "outcome must be WON or LOST", source="outcome")
        if d["status"] in ("ACCEPTED", "WON", "LOST"):
            raise _err(400, "validation_error", "Dispute transition is not supported")
        self._mutate()
        d["status"] = outcome
        d["updated_at"] = self._ts()
        return 200, self._public(d)

    def _report_create(self, body: dict[str, Any], obo: str | None) -> tuple[int, Any]:
        if not body.get("type"):
            raise _err(400, "field_required", "field [type] is required", source="type")
        if not body.get("file_format"):
            raise _err(
                400, "field_required", "field [file_format] is required", source="file_format"
            )
        self._mutate()
        obj = {
            "id": self._uuid("report"),
            "type": body["type"],
            "file_format": body["file_format"],
            "status": "PENDING",
            "_owner": PLATFORM,
            "_t": self._now,
        }
        self._state.kind("platform_reports")[obj["id"]] = obj
        return 201, self._public(obj)

    # ---- reads --------------------------------------------------------------------------------

    def _get(self, path: str, query: dict[str, str], obo: str | None) -> tuple[int, Any]:
        try:
            owner = self._owner(obo)
            return self._get_inner(path, query, owner)
        except _Reply as reply:
            return reply.status, reply.body

    def _owned(self, kind: str, owner: _Account) -> list[dict[str, Any]]:
        return [
            o
            for o in self._state.kind(kind).values()
            if o.get("_owner") == owner.id and self._visible(o)
        ]

    def _listing(
        self,
        kind: str,
        owner: _Account,
        query: dict[str, str],
        filters: tuple[str, ...] = ("request_id", "status"),
    ) -> dict[str, Any]:
        items = self._owned(kind, owner)
        for f in filters:
            if f in query:
                items = [o for o in items if str(o.get(f)) == query[f]]
        if "page_size" in query or "page_num" in query:
            # Sandbox r2 (issuing transactions): page_num from 0, page_size >= 10.
            try:
                size, num = int(query.get("page_size", "100")), int(query.get("page_num", "0"))
            except ValueError:
                # A: the sandbox answer to a non-numeric page parameter is unknown.
                raise _err(400, "invalid_argument", "page parameters must be integers") from None
            if num < 0:
                raise _err(400, "invalid_argument", "page_num must not be negative")
            if size < 10:
                raise _err(400, "invalid_argument", "BELOW_MIN_PAGE_SIZE", source="")
            page = items[num * size : (num + 1) * size]
            more = len(items) > (num + 1) * size
            return {"items": [self._public(o) for o in page], "has_more": more}
        return {"items": [self._public(o) for o in items], "has_more": False}

    def _one(
        self, kind: str, owner: _Account, obj_id: str, not_found: tuple[str, str]
    ) -> dict[str, Any]:
        obj = self._state.kind(kind).get(obj_id)
        if obj is None or obj.get("_owner") != owner.id or not self._visible(obj):
            raise _err(404, not_found[0], not_found[1], source="")
        return self._public(obj)

    def _get_inner(self, path: str, query: dict[str, str], owner: _Account) -> tuple[int, Any]:
        name = read_name(path)
        ids = next((_match(t, path) for t, n in READ_NAMES if n == name), None) or []
        if name == "balances.current":
            return 200, [
                {
                    "currency": ccy,
                    "available_amount": _num(av),
                    "reserved_amount": _num(res),
                    "pending_amount": 0,
                    "prepayment_amount": 0,
                    "total_amount": _num(av + res),
                    "account_type": "cash",
                }
                for ccy, (av, res) in sorted(owner.balances.items())
            ]
        simple = {
            "transfers.list": ("transfers", ("request_id", "status")),
            "beneficiaries.list": (
                "beneficiaries",
                ("nickname",) if self._nickname_filter else (),
            ),
            "refunds.list": ("refunds", ("payment_intent_id", "status")),
            "fx.conversions.list": ("conversions", ("request_id", "status")),
            "global_accounts.list": ("global_accounts", ()),
            "connected_account_transfers.list": (
                "connected_account_transfers",
                ("request_id", "status"),
            ),
            "charges.list": ("charges", ("request_id", "status")),
            "issuing.cardholders.list": ("cardholders", ("email", "cardholder_status")),
            "issuing.cards.list": ("cards", ("cardholder_id",)),
            "issuing.transactions.list": ("card_transactions", ("card_id",)),
            "payment_intents.list": ("payment_intents", ("merchant_order_id",)),
        }
        if name in simple:
            kind, filters = simple[name]
            return 200, self._listing(kind, owner, query, filters)
        gets = {
            "transfers.get": ("transfers", ("not_found", "Transfer not found")),
            "beneficiaries.get": ("beneficiaries", ("not_found", "Beneficiary not found")),
            "fx.conversions.get": ("conversions", ("not_found", "Conversion not found")),
            "global_accounts.get": ("global_accounts", ("not_found", "Global account not found")),
            "connected_account_transfers.get": (
                "connected_account_transfers",
                ("not_found", "Not found"),
            ),
            "charges.get": ("charges", ("not_found", "Not found")),
            "issuing.cardholders.get": ("cardholders", ("NOT_FOUND", "NOT_FOUND")),
            "issuing.cards.get": ("cards", ("NOT_FOUND", "NOT_FOUND")),
            "issuing.transactions.get": ("card_transactions", ("NOT_FOUND", "NOT_FOUND")),
            "payment_intents.get": ("payment_intents", ("not_found", "Not found")),
            "platform_reports.get": ("platform_reports", ("not_found", "Not found")),
        }
        if name in gets:
            kind, nf = gets[name]
            return 200, self._one(kind, owner, ids[0], nf)
        if name == "issuing.cards.limits":
            card = self._one("cards", owner, ids[0], ("NOT_FOUND", "NOT_FOUND"))
            tl = card["authorization_controls"].get("transaction_limits") or {}
            return 200, {
                "currency": tl.get("currency", "USD"),
                "limits": [
                    {
                        "amount": _num(_dec(lim["amount"], "amount")),
                        "interval": lim["interval"],
                        "remaining": _num(
                            _dec(lim["amount"], "amount")
                            - (
                                Decimal(0)
                                if lim["interval"] == "PER_TRANSACTION"
                                else self._spent(card["card_id"], lim["interval"])
                            )
                        ),
                    }
                    for lim in tl.get("limits") or []
                ],
            }
        if name == "fx.rates.current":
            buy, sell = query.get("buy_currency"), query.get("sell_currency")
            if not buy or not sell:
                raise _err(400, "field_required", "buy_currency and sell_currency are required")
            pair, mid, client = self._rate(sell, buy)
            return 200, {
                "buy_currency": buy,
                "sell_currency": sell,
                "currency_pair": pair,
                "rate": client,
                "dealt_currency": buy,
                "conversion_date": self._ts()[:10],
                "rate_details": [
                    {"level": "AWX", "rate": mid},
                    {"level": "CLIENT", "rate": client},
                ],
            }
        if name == "accounts.list":
            if owner.id != PLATFORM:
                raise _err(401, "credentials_invalid", "Access Denied", source="")
            accts = [
                self._account_view(a) for a in self._state.accounts.values() if a.id != PLATFORM
            ]
            return 200, {"items": accts, "has_more": False}
        if name == "accounts.get":
            if owner.id not in (PLATFORM, ids[0]):  # A: a tenant sees only its own account
                raise _err(404, "not_found", "Account not found")
            return 200, self._account_view(self._connected(ids[0]))
        if name == "disputes.list":
            return 200, {"items": [self._public(d) for d in self._owned("disputes", owner)]}
        if name == "disputes.get":
            d = self._dispute(ids[0])
            if d["_owner"] != owner.id or not self._visible(d):
                raise _err(404, "not_found", "not found", details={})
            return 200, self._public(d)
        if name == "financial_transactions.list":
            return 200, self._financial_transactions(query, owner)
        if name == "balances.history":
            return 200, self._balance_history(query, owner)
        return 404, {
            "code": "not_found",
            "message": f"The requested endpoint does not exist [{path}]",
            "details": {},
        }


def _roundtrip(value: Any) -> Any:
    return decode_json(encode_json(value))


class _UnrecoverableReversal(Exception):
    """A FAILED platform movement whose destination no longer holds the amount."""


def _b64id(seed: str) -> str:
    import base64

    digest = hashlib.sha256(seed.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _validation_errors(code: str, source: str) -> _Reply:
    return _Reply(
        400,
        {
            "code": "validation_failed",
            "message": "The request failed our schema validation",
            "details": {"errors": [{"code": code, "source": source}]},
        },
    )


def _insufficient(op_name: str, corr: list[dict[str, str]]) -> tuple[int, dict[str, Any]]:
    if op_name == "transfers.create":
        return 400, {
            "code": "balance_insufficient",
            "details": {},
            "message": "There is insufficient Wallet balance to fund this transfer. Top up your "
            "Wallet balance or provide a lower amount.",
            "source": "transfer",
        }
    if op_name in ("charges.create", "connected_account_transfers.create"):
        return 400, {
            "code": "insufficient_fund",
            "message": "The account has insufficient funds. Please try again when more funds "
            "become available.",
            "source": "charge" if op_name == "charges.create" else "transfer",
        }
    return 400, {
        "code": "insufficient_funds",
        "details": corr,
        "message": "insufficient_funds",
        "source": "conversion",
    }


def _write_names() -> frozenset[str]:
    from .endpoints import ENDPOINTS

    return frozenset(ENDPOINTS) | {"files.upload"}


__all__ = ["READ_NAMES", "FakeAirwallex", "Fault", "read_name"]
