"""Kit 7 agent tools. The model passes a loan_id and an ISO week at most; never an amount,
an account or a request_id."""

from __future__ import annotations

from awx_common import ToolKind, ToolSpec

_LOAN_ID = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{2,40}$"}
_WEEK = {
    "type": "string",
    "pattern": "^([0-9]{4}-W[0-9]{2}|DEMO[0-9]{2,5}S-[0-9]{1,9})$",
    "description": "ISO week, or a labelled DEMO period on a demo portfolio",
}


def _obj(**props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": sorted(props),
        "additionalProperties": False,
    }


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "read_borrower_position",
        ToolKind.READ,
        "The caller's own loan id (your_loan_ids; a borrower has one loan) and that loan's "
        "position from the subledger (principal, fee and arrears outstanding, commitment "
        "remaining, closed weeks) with the caller's current wallet balance. loan_id is "
        "optional: omit it when the caller has not named a loan, and call this first.",
        {
            "type": "object",
            "properties": {"loan_id": _LOAN_ID},
            "required": [],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "read_weekly_collection_preview",
        ToolKind.READ,
        "What the repayment formula would collect from one borrower for one ended ISO week: "
        "revenue, revenue share, cap, floor, amount due, wallet balance and any shortfall.",
        _obj(loan_id=_LOAN_ID, week=_WEEK),
    ),
    ToolSpec(
        "read_portfolio_position",
        ToolKind.READ,
        "Portfolio cash in the ledger and on Airwallex, usable cash, the reserve floor, "
        "tranches in flight, "
        "per-loan totals and the reconciliation result. Platform scope only.",
        _obj(),
    ),
    ToolSpec(
        "propose_collect_weekly_repayment",
        ToolKind.PROPOSE,
        "Collect one borrower's repayment for its next uncollected ISO week, once that week "
        "has ended. Code sizes the charge; a short wallet becomes arrears. Weeks run in "
        "order from the first full week after funding, each once.",
        _obj(loan_id=_LOAN_ID, week=_WEEK),
    ),
    ToolSpec(
        "propose_disburse_advance",
        ToolKind.PROPOSE,
        "Disburse the next tranche of an underwritten advance. Takes only the loan_id; if the "
        "borrower has not named it, call read_borrower_position without a loan_id to find "
        "their own loan ids instead of asking. The "
        "borrower cannot choose the amount. Code sizes the tranche from the loan's remaining "
        "commitment and the platform's lending capacity (full, partial or delayed), whatever "
        "amount the borrower asked for; say so instead of giving another reason.",
        _obj(loan_id=_LOAN_ID),
    ),
    ToolSpec(
        "propose_reconcile_portfolio",
        ToolKind.PROPOSE,
        "Reconcile the loan subledger to the Airwallex movements it sent (by request_id) and "
        "to the platform wallet, record the result, and request a balance report.",
        _obj(),
    ),
)
