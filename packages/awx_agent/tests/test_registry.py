from dataclasses import replace

import pytest
from awx_agent.registry import (
    ToolRegistrationError,
    load_kit_tools,
    sibling_module,
    validate_kit_tools,
)
from awx_common.tools import ToolKind, ToolSpec

_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False}


def _fixture():
    plan = load_kit_tools("k1", "awx_fixture_kit.tools")
    return plan.tools, plan.specs, plan.reads


def test_fixture_kit_registers():
    plan = load_kit_tools("k1", "awx_fixture_kit.tools")
    assert [t.name for t in plan.tools] == [
        "read_balances",
        "propose_pay_supplier",
        "propose_probe_write",
    ]
    assert plan.propose_actions == ("pay_supplier", "probe_write")


def test_rejects_unknown_kit():
    tools, specs, reads = _fixture()
    with pytest.raises(ToolRegistrationError, match="unknown kit"):
        validate_kit_tools("k9", tools, specs, reads)


@pytest.mark.parametrize(
    "field", ["tenant_id", "on_behalf_of", "request_id", "approver", "_awx_caller"]
)
def test_rejects_server_owned_fields_even_if_toolspec_was_mutated(field):
    tools, specs, reads = _fixture()
    bad = replace(tools[0])  # a fresh, valid object
    schema = {
        "type": "object",
        "properties": {"wrapper": {"type": "object", "properties": {field: {"type": "string"}}}},
        "additionalProperties": False,
    }
    object.__setattr__(bad, "input_schema", schema)
    with pytest.raises(ToolRegistrationError, match="server-owned"):
        validate_kit_tools("k1", (bad, *tools[1:]), specs, reads)


def test_toolspec_itself_refuses_server_owned_fields():
    with pytest.raises(ValueError, match="server-owned"):
        ToolSpec(
            name="propose_x",
            kind=ToolKind.PROPOSE,
            description="d",
            input_schema={
                "type": "object",
                "properties": {"connected_account_id": {"type": "string"}},
                "additionalProperties": False,
            },
        )


def test_rejects_propose_without_specs_entry():
    tools, _specs, reads = _fixture()
    with pytest.raises(ToolRegistrationError, match="no SPECS entry"):
        validate_kit_tools("k1", tools, {}, reads)


def test_rejects_spec_of_another_kit():
    tools, specs, reads = _fixture()
    other = replace(specs["pay_supplier"], kit="k2")
    with pytest.raises(ToolRegistrationError, match="another kit"):
        validate_kit_tools("k1", tools, {"pay_supplier": other}, reads)


def test_rejects_read_without_reads_entry():
    tools, specs, _reads = _fixture()
    with pytest.raises(ToolRegistrationError, match="no READS entry"):
        validate_kit_tools("k1", tools, specs, {})


def test_rejects_duplicates_and_non_toolspecs():
    tools, specs, reads = _fixture()
    with pytest.raises(ToolRegistrationError, match="twice"):
        validate_kit_tools("k1", (*tools, tools[0]), specs, reads)
    with pytest.raises(ToolRegistrationError, match="only"):
        validate_kit_tools("k1", (*tools, {"name": "read_x"}), specs, reads)
    with pytest.raises(ToolRegistrationError, match="no tools"):
        validate_kit_tools("k1", (), specs, reads)


def test_rejects_additional_properties_mutated_open():
    tools, specs, reads = _fixture()
    bad = replace(tools[0])
    object.__setattr__(bad, "input_schema", {**_SCHEMA, "additionalProperties": True})
    with pytest.raises(ToolRegistrationError, match="additionalProperties"):
        validate_kit_tools("k1", (bad, *tools[1:]), specs, reads)


def test_tools_module_must_end_in_tools():
    with pytest.raises(ToolRegistrationError, match=r"\.tools"):
        sibling_module("awx_fixture_kit.workflow", "reads")
