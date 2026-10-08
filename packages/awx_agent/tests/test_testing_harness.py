"""awx_agent.testing: a kit's tools through the real ToolDispatcher with no AWS."""

from decimal import Decimal

import pytest
from awx_agent.testing import ADAPTER_FN, KitToolHarness
from awx_common.kits import KITS
from awx_common.money import Money
from awx_spine.fake import FakeAirwallex


@pytest.mark.parametrize("kit", sorted(KITS))
def test_every_kit_loads_into_the_harness_and_refuses_an_unsigned_call(kit):
    h = KitToolHarness.for_kit(kit, port=FakeAirwallex())
    tool = next(iter(h.dispatcher.tools))
    out = h.call(tool, {}, token="not.a.jwt")
    assert out["error"]["code"] == "NOT_AUTHORISED"
    assert h.reader.calls == []
    assert h.sfn.calls == []


def test_k1_read_tool_reads_fake_airwallex_through_the_kit_reader():
    from awx_k1_treasury.book import DEMO_BOOK, seed_book

    port = FakeAirwallex(
        balances={c: Money.of(Decimal("1000000"), c) for c in DEMO_BOOK.currencies}
    )
    h = KitToolHarness.for_kit("k1", port=port)
    seed_book(h.store(), DEMO_BOOK)
    out = h.call("read_balances", {})
    assert "untrusted_data" in out, out
    rows = out["untrusted_data"]["balances"]
    assert {r["currency"] for r in rows} == set(DEMO_BOOK.currencies)
    assert h.reader.calls
    assert {c["FunctionName"] for c in h.reader.calls} != {ADAPTER_FN}
    assert all(c["payload"]["tenant_id"] == "platform" for c in h.reader.calls)


class _FixturePort:
    """The fixture kit reads /api/v1/account, which FakeAirwallex does not model."""

    def read(self, req):
        if req.path == "/api/v1/account":
            return {"id": "acct_platform"}
        return {"items": []}

    def execute(self, op):
        raise AssertionError("the tool plane never executes an operation")


def test_propose_through_the_harness_records_one_waiting_execution():
    h = KitToolHarness.for_kit("k1", port=_FixturePort(), tools_module="awx_fixture_kit.tools")
    args = {"amount": "50.00", "currency": "AUD", "beneficiary_name": "Acme"}
    first = h.call("propose_pay_supplier", args)
    again = h.call("propose_pay_supplier", args)
    assert first["status"] == "AWAITING_APPROVAL"  # agent floor: AUTO policy, model-started
    assert again["proposal_id"] == first["proposal_id"]
    (execution,) = h.executions
    assert execution["origin"] == "AGENT"
    assert execution["proposal"]["proposed_by"] == "user-requester"


def test_harness_rejects_another_kits_reads():
    h = KitToolHarness.for_kit("k5", port=FakeAirwallex(), tenants={"tenant-a": "acct_A"})
    out = h.call(next(n for n in h.dispatcher.tools if n.startswith("read_")), {})
    assert out["error"]["code"] in {"NOT_AUTHORISED", "INVALID_INPUT", "INTERNAL", "TENANT_SCOPE"}


@pytest.mark.parametrize(
    ("status", "outcome", "expected"),
    [
        ("SUCCEEDED", None, "EXISTING_PROPOSAL_EXECUTION_SUCCEEDED"),
        ("FAILED", None, "EXISTING_PROPOSAL_EXECUTION_FAILED"),
        ("SUCCEEDED", "REJECTED", "EXISTING_PROPOSAL_REJECTED"),
    ],
)
def test_a_retry_after_the_execution_closed_reports_the_existing_proposal(
    status, outcome, expected
):
    """The same name and input after the execution closed is ExecutionAlreadyExists in Step
    Functions, so the deployed path reports the existing one."""
    from datetime import UTC, datetime

    fixed = datetime.now(UTC).replace(microsecond=0)  # same clock: the retry's input is identical
    h = KitToolHarness.for_kit(
        "k1", port=_FixturePort(), tools_module="awx_fixture_kit.tools", clock=lambda: fixed
    )
    args = {"amount": "50.00", "currency": "AUD", "beneficiary_name": "Acme"}
    first = h.call("propose_pay_supplier", args)
    (arn,) = h.sfn.executions
    assert h.sfn.calls[0]["input"] == h.sfn.executions[arn]
    h.sfn.status[arn] = status
    if outcome:
        h.outcomes[first["proposal_id"]] = outcome
    again = h.call("propose_pay_supplier", args)
    assert again["proposal_id"] == first["proposal_id"]
    assert again["existing_proposal"] is True
    assert again["status"] == expected
    assert again["execution_status"] == status
    assert len(h.sfn.executions) == 1


# --- the harness signs tokens with its own clock -------------------------------------------


def _k1_harness(clock):
    from awx_k1_treasury.book import DEMO_BOOK, seed_book

    port = FakeAirwallex(
        balances={c: Money.of(Decimal("1000000"), c) for c in DEMO_BOOK.currencies}
    )
    h = KitToolHarness.for_kit("k1", port=port, clock=clock)
    seed_book(h.store(), DEMO_BOOK)
    return h


def test_a_fixed_clock_ten_minutes_in_the_past_still_authorises():
    """The k1 flake: a test fixes NOW at import and runs long after. Signing used real time,
    so iat was 'in the future' for the verifier; the harness now signs with its clock."""
    from datetime import UTC, datetime, timedelta

    then = datetime.now(UTC) - timedelta(minutes=10)
    h = _k1_harness(lambda: then)
    out = h.call("read_balances", {})
    assert "untrusted_data" in out, out
    tok = h.token()
    assert h.call("read_balances", {}, token=tok).get("untrusted_data") is not None


def test_a_fixed_clock_in_the_future_still_authorises():
    from datetime import UTC, datetime, timedelta

    later = datetime.now(UTC) + timedelta(hours=3)
    assert "untrusted_data" in _k1_harness(lambda: later).call("read_balances", {})


def test_an_expired_token_is_still_refused():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    h = _k1_harness(lambda: now)
    stale = h.token(now=(now - timedelta(hours=2)).timestamp())  # exp = iat + 1h, long gone
    out = h.call("read_balances", {}, token=stale)
    assert out["error"]["code"] == "NOT_AUTHORISED"
    assert h.reader.calls == []


def test_a_token_from_before_the_harness_clock_moves_is_refused_after_expiry():
    """A clock that advances past exp refuses the token the harness issued earlier."""
    from datetime import UTC, datetime, timedelta

    t = [datetime.now(UTC) - timedelta(days=1)]
    h = _k1_harness(lambda: t[0])
    tok = h.token()
    assert "untrusted_data" in h.call("read_balances", {}, token=tok)
    t[0] += timedelta(hours=2)
    assert h.call("read_balances", {}, token=tok)["error"]["code"] == "NOT_AUTHORISED"
    assert "untrusted_data" in h.call("read_balances", {})  # a fresh token at the new time


def test_the_harness_issuer_signs_at_the_harness_clock_without_now():
    """h.issuer.token() with no now= also uses the harness clock."""
    from datetime import UTC, datetime, timedelta

    then = datetime.now(UTC) - timedelta(minutes=10)
    h = _k1_harness(lambda: then)
    out = h.call("read_balances", {}, token=h.issuer.token(tenant_id="platform"))
    assert "untrusted_data" in out, out
    assert h.issuer.clock() == then.timestamp()


def test_a_bare_token_issuer_still_signs_at_real_time():
    import time

    from awx_agent.testing import TokenIssuer

    issuer = TokenIssuer()
    assert abs(issuer.clock() - time.time()) < 5
    assert issuer.verifier().verify(issuer.token()).sub == "user-requester"


# --- a platform kit refuses a caller bound to another kit -----------------------------------


@pytest.mark.parametrize("kit", ["k1", "k2", "k3", "k4"])
def test_a_platform_kit_refuses_a_tenant_bound_to_another_kit(kit):
    from awx_common.tenancy import TenantRow

    h = KitToolHarness.for_kit(
        kit,
        port=FakeAirwallex(),
        tenants={
            "k6:operator": TenantRow(kit="k6", connected_account_id=None, role="operator"),
            "k5-cust": TenantRow(kit="k5", connected_account_id="acct_K5", role="customer"),
        },
    )
    tool = next(n for n, t in h.dispatcher.tools.items() if t.kind.name == "READ")
    for tenant in ("k6:operator", "k5-cust"):
        out = h.call(tool, {}, tenant_id=tenant)
        assert out.get("error", {}).get("code") == "TENANT_SCOPE", (kit, tenant, out)
    assert h.reader.calls == []
    assert h.sfn.calls == []


def test_the_platform_demo_users_reach_kit1_and_unrowed_or_missing_claims_do_not():
    from awx_k1_treasury.book import DEMO_BOOK, seed_book

    port = FakeAirwallex(
        balances={c: Money.of(Decimal("1000000"), c) for c in DEMO_BOOK.currencies}
    )
    h = KitToolHarness.for_kit("k1", port=port)
    seed_book(h.store(), DEMO_BOOK)
    for tenant in ("platform", None):  # None: the harness's kit 1-4 default, "platform"
        out = h.call("read_balances", {}, tenant_id=tenant)
        assert "untrusted_data" in out, (tenant, out)
    calls = len(h.reader.calls)
    for tok in (h.token(tenant_id="no-such-row"), h.issuer.token()):  # unrowed; no claim
        out = h.call("read_balances", {}, token=tok)
        assert out["error"]["code"] == "TENANT_SCOPE", out
    assert len(h.reader.calls) == calls
