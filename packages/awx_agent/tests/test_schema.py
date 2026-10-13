import pytest
from awx_agent.registry import load_kit_tools
from awx_agent.schema import ToolInputError, check_arguments, to_gateway_schema, tool_definition


def _propose():
    return load_kit_tools("k1", "awx_fixture_kit.tools").tools[1]


def test_gateway_schema_keeps_only_supported_keywords_and_folds_the_rest():
    d = tool_definition(_propose())
    props = d["inputSchema"]["properties"]
    assert set(d["inputSchema"]) == {"type", "properties", "required"}
    assert "one of AUD, USD" in props["currency"]["description"]
    assert "pattern=" in props["amount"]["description"]
    assert props["lines"]["items"]["properties"]["ref"] == {"type": "string"}
    assert d["description"].endswith(
        " Creates a proposal only. Code sets its risk tier when you propose."
    )
    # The identity field is a declared, optional property the server fills.
    assert props["_awx_caller"]["type"] == "string"
    assert "_awx_caller" not in d["inputSchema"]["required"]


def test_gateway_schema_refuses_untyped_nodes():
    with pytest.raises(ValueError, match="no Gateway equivalent"):
        to_gateway_schema({"properties": {}})
    assert to_gateway_schema({"type": ["string", "null"]}) == {"type": "string"}


def test_check_arguments():
    spec = _propose()
    ok = {"amount": "10.00", "currency": "AUD", "beneficiary_name": "Acme"}
    assert check_arguments(spec, ok) == ok
    with pytest.raises(ToolInputError, match="server"):
        check_arguments(spec, {**ok, "tenant_id": "t2"})
    with pytest.raises(ToolInputError, match="server"):
        check_arguments(spec, {**ok, "lines": [{"request_id": "x"}]})
    with pytest.raises(ToolInputError, match="unknown"):
        check_arguments(spec, {**ok, "extra": 1})
    with pytest.raises(ToolInputError, match="missing"):
        check_arguments(spec, {"amount": "1.00"})
    with pytest.raises(ToolInputError, match="decimal strings"):
        check_arguments(spec, {**ok, "amount": 10.5})
