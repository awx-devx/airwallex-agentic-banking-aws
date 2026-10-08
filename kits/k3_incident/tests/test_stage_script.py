"""`scripts/stage_original_transfer.py` against FakeAirwallex: it stages the original with
an approval decision, so the transfer is created and left in flight, and the agent's
assessment reads it as WAIT. A run with no approval sends nothing."""

from __future__ import annotations

import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
from awx_spine.fake import EPOCH
from k3_fixtures import Kit3

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage_original_transfer.py"


def _load():
    spec = importlib.util.spec_from_file_location("stage_original_transfer", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():
    return _load()


def _run(script, kit, tmp_path, *argv, confirm=None):
    return script.main(
        ["--lock", str(tmp_path / "lock"), *argv],
        port=kit.fake,
        confirm=confirm,
        now=lambda: EPOCH + timedelta(seconds=kit.fake.clock()),
        sleep=kit.fake.advance,
    )


def test_the_script_stages_an_original_the_agent_reads_as_wait(script, tmp_path, capsys):
    kit = Kit3.new()
    shown: list[str] = []

    def approve(text: str) -> bool:
        shown.append(text)
        return True

    assert _run(script, kit, tmp_path, "--payment-key", "inv-script-1", confirm=approve) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "SUCCEEDED"
    assert report["tier"] == "SINGLE_APPROVER"
    oid = report["transfer_id"]
    assert report["reference"].endswith("-nosettle")
    assert report["status"] in ("SCHEDULED", "PROCESSING", "SENT")
    assert oid in report["next"]
    (approval,) = shown
    assert '"account_last4"' in approval  # the payee the approver checks
    assert len(kit.sends()) == 1
    assessment = kit.read("read_incident_assessment", {"transfer_id": oid})
    assert assessment["decision"] == "WAIT"


def test_without_an_approval_nothing_is_sent(script, tmp_path, capsys):
    kit = Kit3.new()
    assert _run(script, kit, tmp_path, confirm=lambda _shown: False) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "REJECTED"
    assert report["transfer_id"] is None
    assert kit.sends() == []


def test_yes_approves_and_the_beneficiary_is_found_by_nickname(script, tmp_path, capsys):
    kit = Kit3.new()
    assert _run(script, kit, tmp_path, "--yes") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "SUCCEEDED"
    (transfer,) = [t for t in kit.fake.objects("transfers") if t["id"] == report["transfer_id"]]
    assert transfer["beneficiary_id"] == kit.beneficiary_id


def test_staging_policy_still_applies(script, tmp_path, capsys):
    kit = Kit3.new()
    assert _run(script, kit, tmp_path, "--yes", "--amount", "10.01") == 1
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "BLOCKED"
    assert any("at most 10.00" in r for r in report["reasons"])
    assert kit.sends() == []


def test_the_key_and_reference_are_printed_before_the_send(script, tmp_path, capsys):
    """So a person can find the transfer after a crash: the key and reference are on stderr by
    the time the operator is asked, and nothing has been sent yet."""
    kit = Kit3.new()
    at_prompt: list[str] = []

    def approve(_shown: str) -> bool:
        assert kit.sends() == []
        at_prompt.append(capsys.readouterr().err)
        return True

    assert _run(script, kit, tmp_path, "--payment-key", "inv-crash-1", confirm=approve) == 0
    report = json.loads(capsys.readouterr().out)
    (printed,) = at_prompt
    assert "payment_key inv-crash-1" in printed
    assert f"transfer reference {report['reference']}" in printed
    assert report["reference"] == "poc-i3-inv-crash-1-nosettle"


def test_the_same_payment_key_on_a_rerun_sends_a_new_transfer(script, tmp_path, capsys):
    """Each run mints a new book, so --payment-key does not make a re-run idempotent."""
    kit = Kit3.new()
    reports = []
    for _ in range(2):
        assert _run(script, kit, tmp_path, "--yes", "--payment-key", "inv-same") == 0
        reports.append(json.loads(capsys.readouterr().out))
    first, second = reports
    assert first["transfer_id"] != second["transfer_id"]
    assert len(kit.sends()) == 2
