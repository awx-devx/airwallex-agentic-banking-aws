"""Offline checks of the live acceptance run's set-up (test_live_sandbox.py), on the fake.

The live run tops employer b up to 0.01 USD short of its payroll and never drains it, so on a
reused sandbox b can already hold more than its payroll. These tests prove the run sizes b's
payroll from b's balance instead of stopping, and that the accounts file parses as the README
describes. They also check the sample payroll file in examples/.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import test_live_sandbox as live
import yaml
from awx_k6_payroll.model import parse_batch
from k6_data import batch

KIT = Path(__file__).resolve().parents[1]
REPO = KIT.parents[1]

pytestmark = pytest.mark.skipif(live.LIVE, reason="offline checks; AWX_K6_LIVE is set")


def _usd_line(employer: dict) -> Decimal:
    return next(
        Decimal(c["amount"]["amount"])
        for c in employer["contractors"]
        if c["amount"]["currency"] == "USD"
    )


def test_b_below_its_payroll_keeps_the_payroll():
    b = batch()["employers"][1]
    sized, short = live.size_short_employer(b, Decimal("3548.76"), Decimal("100.00"))
    assert sized is None
    assert short == Decimal("3548.75")


def test_b_above_its_payroll_grows_the_usd_payout_by_the_difference():
    b = batch()["employers"][1]
    sized, short = live.size_short_employer(b, Decimal("3548.76"), Decimal("3560.48"))
    assert sized is not None
    assert short == Decimal("3560.48")  # b's balance is already 0.01 short: nothing to deposit
    assert _usd_line(sized) - _usd_line(b) == Decimal("11.73")
    assert b["contractors"][0]["amount"]["amount"] == "900.00"  # the input is not mutated
    others = [c for c in sized["contractors"] if c["amount"]["currency"] != "USD"]
    assert others == [c for c in b["contractors"] if c["amount"]["currency"] != "USD"]


def test_b_exactly_one_cent_short_needs_no_change():
    b = batch()["employers"][1]
    sized, short = live.size_short_employer(b, Decimal("3548.76"), Decimal("3548.75"))
    assert sized is None
    assert short == Decimal("3548.75")


def test_b_without_a_usd_local_payout_stops():
    b = batch()["employers"][1]
    b["contractors"] = [c for c in b["contractors"] if c["amount"]["currency"] != "USD"]
    with pytest.raises(SystemExit, match="no USD LOCAL payout"):
        live.size_short_employer(b, Decimal("10.00"), Decimal("50.00"))


def test_growth_at_the_cap_is_allowed():
    b = batch()["employers"][1]
    need = Decimal("3548.76")
    sized, _ = live.size_short_employer(b, need, need - Decimal("0.01") + live.MAX_GROWTH)
    assert _usd_line(sized) - _usd_line(b) == live.MAX_GROWTH


def test_growth_above_the_cap_stops_with_a_clear_message():
    b = batch()["employers"][1]
    with pytest.raises(SystemExit, match=r"grows b's payroll by at most 1000\.00 USD"):
        live.size_short_employer(b, Decimal("3548.76"), Decimal("4548.76"))
    assert live.MAX_GROWTH == Decimal("1000.00")


def test_a_large_stray_deposit_into_b_stops_the_run_with_nothing_sent(tmp_path):
    live.log.clear()
    outcome, problems = live.acceptance(
        fake_opening={"b": "20000.00"}, lock_path=str(tmp_path / "lock")
    )
    assert outcome == "STOP", problems
    assert "at most 1000.00 USD" in problems[0]
    assert not [e for e in live.log if e.get("step") in ("deposit", "payroll")]


def test_rerun_passes_when_b_already_holds_more_than_its_payroll(tmp_path):
    """The 8 Oct 2026 state: b held 3560.48 USD, above its 3548.75 target. The run must pass
    without moving money out of b and without depositing into b."""
    live.log.clear()
    outcome, problems = live.acceptance(
        fake_opening={"b": "3560.48"}, lock_path=str(tmp_path / "lock")
    )
    assert outcome == "PASS", problems
    b_deposits = [e for e in live.log if e.get("step") == "deposit" and e["employer"] == "b"]
    assert b_deposits == []
    (lock,) = [e for e in live.log if e.get("step") == "lock"]
    assert lock["path"] == str(tmp_path / "lock")  # never the shared /tmp/awx-sandbox.lock
    (need_b,) = [e for e in live.log if e.get("step") == "need_b"]
    assert need_b["b_balance"] == "3560.48"
    assert "short" not in need_b


def test_accounts_file_format(tmp_path, monkeypatch):
    f = tmp_path / "k6-accounts"
    f.write_text(
        "# kit 6 employer accounts\n"
        "K6_EMPLOYER_A_ACCT: acct_aaa\n"
        "K6_EMPLOYER_B_ACCT: acct_bbb  # poc-i6-b\n"
        "K6_EMPLOYER_C_ACCT: acct_ccc\n"
    )
    monkeypatch.setattr(live, "IDS", str(f))
    assert live.load_ids() == {"a": "acct_aaa", "b": "acct_bbb", "c": "acct_ccc"}


def test_sample_payroll_parses_and_names_deployed_employers():
    sample = json.loads((KIT / "examples" / "payroll-2026-W41.json").read_text())
    parsed = parse_batch(sample)
    deploy = yaml.safe_load((REPO / "deploy" / "k6.yaml").read_text())
    deployed = {
        row["tenant_id"].rsplit(":", 1)[1]
        for row in deploy["tenants"]
        if row["tenant_id"].startswith("k6:employer:")
    }
    assert {e["employer"] for e in parsed["employers"]} <= deployed
    corridors = {
        (c["address"]["country_code"], c["amount"].currency, c["transfer_method"])
        for e in parsed["employers"]
        for c in e["contractors"]
    }
    assert corridors == {("US", "USD", "LOCAL"), ("GB", "GBP", "LOCAL"), ("DE", "EUR", "SWIFT")}
