"""Deterministic request IDs, scoped to a minted book (kit rule M2) and an attempt (rule M1).

Every business key starts with the kit's book id: a random id minted once, when kit 6's
platform state is first created, and stored with it (`books.py`). Employer, contractor and
pay period come from seed data, so without the book a second demo run on the shared sandbox
would re-derive the first run's request_ids and adopt its payouts.

The same business action in one book always yields the same request_id, so a rerun of a pay
period re-presents the same ids and can never pay twice. After a definitive failure (a 4xx at
create, or a CANCELLED transfer) the next attempt for that money gets its own segment,
`<step>:<attempt>`, recorded in the store; nothing else ever mints a new id for that money
(rule M1). Quote ids also carry the minute: fx/quotes/create ignores request_id, so the id
only names the quote in the operation store and moves no money.
"""

from __future__ import annotations

import re
from datetime import datetime

from awx_common import derive_request_id

from .model import KIT

BOOK = re.compile(r"^[0-9a-f]{32}$")


def _book(book: str) -> str:
    if not isinstance(book, str) or not BOOK.match(book):
        raise ValueError("book id must be a minted 32-hex id, never a constant or default")
    return book


def _attempt(attempt: int) -> int:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be an int >= 1")
    return attempt


def payout_key(book: str, employer: str, contractor: str, pay_period: str) -> str:
    return f"payout:{_book(book)}:{employer}:{contractor}:{pay_period}"


def payout_request_id(
    book: str, employer: str, contractor: str, pay_period: str, attempt: int
) -> str:
    key = payout_key(book, employer, contractor, pay_period)
    return derive_request_id(KIT, key, f"transfer:{_attempt(attempt)}")


def conversion_key(book: str, employer: str, pay_period: str, buy: str, sell: str) -> str:
    return f"fx:{_book(book)}:{employer}:{pay_period}:{sell}->{buy}"


def conversion_request_id(
    book: str, employer: str, pay_period: str, buy: str, sell: str, attempt: int
) -> str:
    key = conversion_key(book, employer, pay_period, buy, sell)
    return derive_request_id(KIT, key, f"conversion:{_attempt(attempt)}")


def quote_request_id(conversion_request: str, conversion_business_key: str, at: datetime) -> str:
    """Names one quote for one conversion attempt in the operation store (the endpoint itself
    ignores request_id). A new attempt or a later minute gets a fresh quote."""
    step = f"quote-{at.strftime('%Y%m%dT%H%M')}:{conversion_request}"
    return derive_request_id(KIT, conversion_business_key, step)


def fee_key(book: str, employer: str, pay_period: str) -> str:
    return f"fee:{_book(book)}:{employer}:{pay_period}"


def fee_request_id(book: str, employer: str, pay_period: str, attempt: int) -> str:
    return derive_request_id(
        KIT, fee_key(book, employer, pay_period), f"charge:{_attempt(attempt)}"
    )


def beneficiary_key(book: str, employer: str, contractor: str) -> str:
    return f"beneficiary:{_book(book)}:{employer}:{contractor}"


def beneficiary_request_id(
    book: str, employer: str, contractor: str, pay_period: str, step: str
) -> str:
    # Beneficiaries persist across periods, but Airwallex refuses a request_id reused within 7
    # days and accepts it after, so the period goes into the step to keep each create unique.
    return derive_request_id(
        KIT, beneficiary_key(book, employer, contractor), f"{step}:{pay_period}"
    )


def beneficiary_nickname(book: str, employer: str, contractor: str) -> str:
    """Also book-scoped, so a fresh book never adopts an earlier run's beneficiary."""
    return f"poc-i6-{_book(book)[:16]}-{employer}-{contractor}"
