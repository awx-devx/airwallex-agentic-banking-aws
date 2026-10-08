"""Exact (action, resource) checks for the roles KitStack builds, resolved from stack attributes.

Each IAM Resource in a synthesized template is normalised to a string: an ImportValue becomes
the symbol of the producer attribute it exports (`{ledger}`, `{reader_k4}`, `{sm_k4_accept}`),
found by resolving the SpineStack / KitStack attribute in its own stack and matching it against
that stack's exported Outputs. So nothing depends on logical-ID substrings, and a literal ARN
(which has no symbol) or a `*` can never match an allowed pair. Actions compare lower-case.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

PSEUDO = {"AWS::Partition", "AWS::Region", "AWS::AccountId", "AWS::URLSuffix"}
# Only these services may be granted on "*": they have no resource-level permissions.
STAR_SERVICES = ("logs:", "xray:", "cloudwatch:")


def export_symbols(producer, template: Mapping[str, Any], attrs: Mapping[str, Any]) -> dict:
    """{export name: symbol} for each attribute that the producer stack exports."""
    outputs = template.get("Outputs", {})
    out = {}
    for symbol, value in attrs.items():
        resolved = producer.resolve(value)
        for o in outputs.values():
            if o.get("Value") == resolved and "Export" in o:
                out[o["Export"]["Name"]] = symbol
    return out


def local_symbols(stack, attrs: Mapping[str, Any]) -> dict:
    """{json of the resolved value: symbol} for attributes used inside their own stack."""
    out = {}
    for symbol, value in attrs.items():
        resolved = stack.resolve(value)
        if not isinstance(resolved, str):
            out[json.dumps(resolved, sort_keys=True)] = symbol
    return out


def norm(value: Any, symbols: Mapping[str, str]) -> str:
    if isinstance(value, str):
        return value
    local = symbols.get(json.dumps(value, sort_keys=True))
    if local is not None:
        return "{" + local + "}"
    if isinstance(value, Mapping) and len(value) == 1:
        ((k, v),) = value.items()
        if k == "Fn::ImportValue":
            return "{" + symbols.get(v, f"?import:{v}") + "}"
        if k == "Ref":
            return "${" + v.split("::")[-1] + "}" if v in PSEUDO else f"@{v}"
        if k == "Fn::GetAtt":
            return "@" + ".".join(v if isinstance(v, list) else [v])
        if k == "Fn::Join":
            sep, parts = v
            return sep.join(norm(p, symbols) for p in parts)
        if k == "Fn::Select":
            return f"sel({v[0]},{norm(v[1], symbols)})"
        if k == "Fn::Split":
            return f"split({v[0]},{norm(v[1], symbols)})"
    return "?" + json.dumps(value, sort_keys=True)


def _list(v):
    return v if isinstance(v, list) else [v]


BASIC_EXECUTION = {
    "Fn::Join": [
        "",
        [
            "arn:",
            {"Ref": "AWS::Partition"},
            ":iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        ],
    ]
}
MANAGED = "managed-policy"  # synthetic action: an attached managed policy, as a pair


# Cross-stack role attachments that exist by design: a kit stack's ApprovalDescribe policy on
# the spine's approval role (kit_workflow.KitWorkflow). Any other import is refused.
ALLOWED_IMPORTED_ROLES = frozenset({"approval_role_name"})


def _ref(value: Any, where: str, symbols: Mapping[str, str]) -> str:
    """An attachment must be a {"Ref": <logical id>} (or an import of an allowed spine role,
    returned as "{symbol}"); anything else is refused, never ignored."""
    if isinstance(value, Mapping) and set(value) == {"Ref"} and isinstance(value["Ref"], str):
        return value["Ref"]
    if isinstance(value, Mapping) and set(value) == {"Fn::ImportValue"}:
        symbol = symbols.get(value["Fn::ImportValue"])
        if symbol in ALLOWED_IMPORTED_ROLES:
            return "{" + symbol + "}"
    raise AssertionError(f"{where} attaches to {value!r}, not a Ref to a role in this stack")


def role_statements(
    template: Mapping[str, Any],
    role_lid_prefix: str | None,
    symbols: Mapping[str, str] | None = None,
    *,
    imported_role: str | None = None,
) -> list[dict]:
    """Every permission of the roles whose logical id starts with `role_lid_prefix`: attached
    Policy and ManagedPolicy documents, RolePolicy, inline Role policies, and ManagedPolicyArns
    (AWSLambdaBasicExecutionRole becomes the pair (managed-policy, AWSLambdaBasicExecutionRole);
    any other ARN is refused; a Ref to a local ManagedPolicy contributes its document).

    Every Policy, ManagedPolicy and RolePolicy in the template must attach by Ref; one that
    names a role any other way (a literal name, an import) is refused, wherever it points."""
    symbols = symbols or {}
    resources = template["Resources"]
    out: list[dict] = []
    roles = {
        lid
        for lid, r in resources.items()
        if r["Type"] == "AWS::IAM::Role"
        and role_lid_prefix is not None
        and lid.startswith(role_lid_prefix)
    }
    if imported_role is not None:
        roles.add("{" + imported_role + "}")
    for lid, r in resources.items():
        p = r.get("Properties", {})
        if r["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            attached = {_ref(x, lid, symbols) for x in _list(p.get("Roles", []) or [])}
            for kind in ("Users", "Groups"):
                if p.get(kind):
                    raise AssertionError(f"{lid} attaches to {kind}")
            if attached & roles:
                out += _list(p["PolicyDocument"]["Statement"])
        elif r["Type"] == "AWS::IAM::RolePolicy":
            if _ref(p.get("RoleName"), lid, symbols) in roles:
                out += _list(p["PolicyDocument"]["Statement"])
        elif r["Type"] in (
            "AWS::IAM::UserPolicy",
            "AWS::IAM::GroupPolicy",
            "AWS::IAM::User",
            "AWS::IAM::Group",
        ):
            raise AssertionError(f"{lid}: {r['Type']} is not allowed")
        if r["Type"] == "AWS::IAM::Role" and lid in roles:
            for pol in p.get("Policies", []) or []:
                out += _list(pol["PolicyDocument"]["Statement"])
            for arn in p.get("ManagedPolicyArns", []) or []:
                if arn == BASIC_EXECUTION:
                    out.append(
                        {
                            "Effect": "Allow",
                            "Action": MANAGED,
                            "Resource": "AWSLambdaBasicExecutionRole",
                        }
                    )
                elif (
                    isinstance(arn, Mapping)
                    and set(arn) == {"Ref"}
                    and resources.get(arn["Ref"], {}).get("Type") == "AWS::IAM::ManagedPolicy"
                ):
                    out += _list(resources[arn["Ref"]]["Properties"]["PolicyDocument"]["Statement"])
                else:
                    raise AssertionError(f"{lid} carries managed policy {arn!r}")
    return out


def pairs(
    statements: Iterable[Mapping[str, Any]],
    symbols: Mapping[str, str],
    *,
    star_ok: Iterable[str] = (),
) -> set:
    """{(action lower, normalised resource, condition json)} with structural refusals."""
    out = set()
    for s in statements:
        assert s.get("Effect", "Allow") == "Allow", s
        assert "NotAction" not in s, s
        assert "NotResource" not in s, s
        cond = json.dumps(s.get("Condition") or {}, sort_keys=True)
        for action in _list(s["Action"]):
            a = str(action).lower()
            for res in _list(s["Resource"]):
                r = norm(res, symbols)
                if r == "*":
                    assert a.startswith(STAR_SERVICES) or a in set(star_ok), (a, "Resource *")
                assert "?import:" not in r, (a, r, "an import of an unknown attribute")
                if a == MANAGED:
                    assert res == "AWSLambdaBasicExecutionRole", res
                elif isinstance(res, str) and res != "*":
                    raise AssertionError(f"literal ARN {res} for {a}")
                out.add((a, r, cond))
    return out


def check_exact(actual: set, allowed: set, patterns: Iterable[tuple[str, str]] = ()) -> None:
    """Every actual (action, resource) pair is allowed exactly, or by (action, regex) pattern
    (own log group only). Conditions are compared where `allowed` gives one ("" = any)."""
    pats = [(a, re.compile(p)) for a, p in patterns]
    for a, r, cond in actual:
        if (a, r, cond) in allowed or (a, r, "") in allowed:
            continue
        if any(a == pa and rx.fullmatch(r) for pa, rx in pats):
            continue
        raise AssertionError(f"not allowed: {a} on {r} with {cond}")
