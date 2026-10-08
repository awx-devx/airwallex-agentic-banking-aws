from datetime import UTC, datetime
from decimal import Decimal

import pytest
from awx_common import Money, RiskTier, canonical_json, derive_request_id, intent_hash, transport


def test_canonical_json_is_order_independent_and_typed():
    a = {"b": Money.of("1", "AUD"), "a": [RiskTier.AUTO, Decimal("2.50")]}
    b = {"a": [RiskTier.AUTO, Decimal("2.50")], "b": Money.of("1.00", "AUD")}
    assert canonical_json(a) == canonical_json(b)
    assert canonical_json(a) == '{"a":["AUTO","2.50"],"b":{"amount":"1.00","currency":"AUD"}}'
    assert intent_hash(a) == intent_hash(b)


def test_intent_hash_is_stable():
    # Golden value: changing it breaks every stored approval binding.
    assert intent_hash({"kit": "k4", "amount": Money.of("10", "USD")}) == (
        "028729161f832b3e0b40530b210d5d80c060ab35d051dea5dbd7fdaf0d6d32db"
    )


@pytest.mark.parametrize(
    "bad",
    [{"x": 1.5}, {"x": {1, 2}}, {"x": datetime(2026, 10, 6)}, {1: "x"}],  # noqa: DTZ001
)
def test_canonical_refuses_ambiguous_values(bad):
    with pytest.raises((TypeError, ValueError)):
        canonical_json(bad)


def test_request_id_is_deterministic_and_scoped():
    first = derive_request_id("k6", "employer-1:2026-W41")
    assert first == derive_request_id("k6", "employer-1:2026-W41")
    assert first != derive_request_id("k6", "employer-1:2026-W41", "replacement-1")
    assert first != derive_request_id("k5", "employer-1:2026-W41")
    with pytest.raises(ValueError, match="required"):
        derive_request_id("", "x")
    with pytest.raises(ValueError, match="separator"):
        derive_request_id("k1", "a\x1fb")


def test_transport_round_trips_exact_values():
    payload = {
        "amount": Decimal("12345678901234567890.12"),
        "fee": Money.of("0.01", "AUD"),
        "at": datetime(2026, 10, 6, 4, 0, tzinfo=UTC),
        "tier": RiskTier.DUAL,
        "items": [Decimal("1.10"), "x", 3, None, True],
    }
    text = transport.dumps(payload)
    back = transport.loads(text)
    assert back["amount"] == payload["amount"]
    assert back["fee"] == payload["fee"]
    assert back["at"] == payload["at"]
    assert back["tier"] == "DUAL_APPROVER"
    assert back["items"] == [Decimal("1.10"), "x", 3, None, True]


def test_transport_refuses_floats_both_ways():
    with pytest.raises(TypeError):
        transport.dumps({"x": 0.1})
    with pytest.raises(TypeError):
        transport.loads('{"x": 0.1}')
    with pytest.raises(ValueError, match="reserved"):
        transport.dumps({"$decimal": "1"})
