"""The live acceptance run's evidence file and failure messages, checked offline.

The live run (`test_acceptance.py`, AWX_LIVE=1) writes `AWX_K1_SHAPES` through `evidence_file`
and builds every failure message with `explain`.
"""

from __future__ import annotations

import json

import pytest
from awx_common import Money

LONG_POST = (
    "reserve floor breached in ['EUR']; ob-supplier-eur PAID; "
    + "the provider shows every leg; " * 20
)


def _run() -> dict:
    return {
        "label": "supported",
        "state": "POSTCONDITION_FAILED",
        "tier": "AUTO",
        "detail": None,
        "post": LONG_POST,
        "end_balances": {"EUR": Money.of("499.99", "EUR")},
    }


def test_a_failure_message_carries_the_full_postcondition(explain):
    run = _run()
    with pytest.raises(AssertionError) as caught:
        assert run["state"] == "SUCCEEDED", explain(run)
    message = str(caught.value)
    assert LONG_POST in message
    assert "499.99" in message


def test_a_failed_run_still_writes_its_evidence(tmp_path, evidence_file):
    path = tmp_path / "shapes.json"
    seen = {"runs": [_run()]}
    with pytest.raises(AssertionError), evidence_file(seen, str(path)):
        raise AssertionError("reserve floor breached in ['EUR']")
    written = json.loads(path.read_text())
    assert written["outcome"] == "failed"
    assert "reserve floor breached" in written["failure"]
    assert written["runs"][0]["post"] == LONG_POST


def test_a_passing_run_writes_its_evidence_and_no_path_writes_nothing(tmp_path, evidence_file):
    path = tmp_path / "shapes.json"
    with evidence_file({"runs": []}, str(path)):
        pass
    assert json.loads(path.read_text()) == {"outcome": "passed", "runs": []}
    with evidence_file({"runs": []}, None):
        pass
    assert [p.name for p in tmp_path.iterdir()] == ["shapes.json"]
