"""A run survives the Step Functions round trip exactly: no float, no lost digit."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from awx_common import Money, Origin, RiskTier
from awx_common.tenancy import TenantContext
from awx_common.transport import decode, dumps, encode, loads
from awx_spine_aws import engine
from hypothesis import given
from hypothesis import strategies as st

import toy


class Env:
    sandbox = True

    def __init__(self, port):
        self.port, self.clock = port, toy.Clock()

    def now(self):
        return self.clock.now()

    def read(self, kit, req, tenant):
        return self.port.read(req)

    def read_platform(self, kit, req, tenant):
        return self.port.read(req)

    def sleep(self, seconds):
        pass

    def read_budget_s(self):
        return None

    def store(self, kit, tenant):
        return None

    def resolve_tenant(self, tenant_id):
        return TenantContext(tenant_id)


def _run_after_build(amount: str) -> engine.Run:
    spec = toy.toy_spec(RiskTier.AUTO)
    env = Env(toy.ToyPort(balance=Money.of("99999999.99", "AUD")))
    tenant = TenantContext("t1", "acct_1")
    raw = {"payee": "ben_1", "amount": amount}
    proposal, decision = engine.propose(
        spec, (), raw, proposed_by="u", tenant=tenant, env=env, origin=Origin.SCHEDULE
    )
    execution = decode(
        json.loads(json.dumps(encode(engine.execution_input(proposal, decision, raw, "t1"))))
    )
    run = engine.start(spec, execution, tenant=tenant)
    run = engine.recheck(spec, run, env)
    return engine.build(spec, run, "pay", env)


def _through_step_functions(run: engine.Run) -> engine.Run:
    """What happens between two states: encode, JSON text, Lambda's json.loads, decode."""
    text = json.dumps(engine.to_payload(run))
    return engine.from_payload(decode(json.loads(text)))


@given(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("9999999.99"), places=2))
def test_amounts_round_trip_exactly(amount):
    run = _run_after_build(format(amount, "f"))
    back = _through_step_functions(run)
    assert back == run
    assert back.step.pending.op.body["transfer_amount"] == amount + Decimal("1.50")
    back.proposal.verify_hash()  # the approval binding survives the trip


def test_payload_has_no_json_numbers_on_money_paths():
    run = _run_after_build("123.45")
    text = json.dumps(engine.to_payload(run))
    assert '"$money"' in text and '"$decimal"' in text
    assert "123.45," not in text and ": 123.45" not in text


def test_derived_fields_for_choice_states():
    data = engine.to_payload(_run_after_build("1.00"))
    assert data["step"]["pending"]["op"]["name"] == "transfers.create"
    assert data["step"]["pending"]["action"] == "send"  # Dispatch reads this
    assert data["step"]["stage"] == "ops"  # Route reads this
    assert data["state"] is None  # Choice IsString on $.run.state relies on null here
    assert data["postcondition_due"] is True


def test_a_float_inside_a_state_payload_is_refused():
    data = encode({"amount": Decimal("1.10")})
    data["amount"] = 1.1
    with pytest.raises(TypeError):
        decode(data)
    with pytest.raises(TypeError):
        loads('{"amount": 1.1}')


def test_dumps_loads_identity_for_a_full_run():
    run = _run_after_build("5.00")
    assert engine.from_payload(loads(json.dumps(engine.to_payload(run)))) == run
    assert loads(dumps(run)) is not None  # the raw run also transports
