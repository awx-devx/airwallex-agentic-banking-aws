"""Kit manifests: what the CDK code knows about a kit, produced OUTSIDE the CDK process.

Kit code never runs in the CDK process. For each kit in a build, `load_manifests` runs this
module as a subprocess (`python -m infra.kit_manifest <kit>`, same interpreter). The
subprocess imports the kit, refuses the unsupported `infra`/`extend` hook, checks TOOLS,
SPECS, READS and FUNCTIONS exactly as the Lambda runtime will, and prints a JSON manifest of
only the fields the infra code reads. The CDK process parses that JSON with a strict schema
(unknown keys, wrong types and the synth limits on tool schemas, tool-name length and SPECS
or READS entries fail here, before any construct exists) and builds from data alone. Whatever
kit code does at import time happens in a process that holds no construct tree.

This module imports no CDK and no kit at module level, so the parent process stays clean.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from awx_common.spec_rules import check_finality_bounds, check_workflow_shape

SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[1]
_GATEWAY_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object"})


class ManifestError(ValueError):
    """A kit manifest is malformed or breaks a synth limit. Raised before any construct exists."""


class RemovedExtensionHook(ManifestError):
    """A kit ships the unsupported free-form `infra.extend()` hook."""


# --- the manifest types (pure data) -------------------------------------------------------


@dataclass(frozen=True)
class FinalityManifest:
    timeout_s: int
    interval_s: int


@dataclass(frozen=True)
class StepManifest:
    name: str
    finality: FinalityManifest | None


@dataclass(frozen=True)
class WorkflowManifest:
    """The fields of a KitWorkflowSpec the CDK code reads. Attribute names match the spec, so
    engine.allowlist, engine.platform_allowlist and engine.child_budget_s accept it."""

    kit: str
    action: str
    approval_ttl: timedelta
    steps: tuple[StepManifest, ...]
    read_allowlist: tuple[str, ...]
    platform_read_allowlist: tuple[str, ...]
    child: WorkflowManifest | None


@dataclass(frozen=True)
class ToolManifest:
    name: str
    kind: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class FunctionManifest:
    name: str
    handler: str
    grants: tuple[str, ...]
    timeout_s: int
    memory_mb: int
    description: str


@dataclass(frozen=True)
class KitManifest:
    kit: str
    package: str
    workflows: Mapping[str, WorkflowManifest]
    tools: tuple[ToolManifest, ...]
    reads: tuple[str, ...]
    functions: tuple[FunctionManifest, ...]
    stand_in: bool = False  # True only for a test's in-process stand-in (build_manifest)

    @property
    def spec_module(self) -> str:
        return f"{self.package}.workflow"

    @property
    def tools_module(self) -> str:
        return f"{self.package}.tools"

    @property
    def propose_actions(self) -> tuple[str, ...]:
        return tuple(t.name.removeprefix("propose_") for t in self.tools if t.kind == "PROPOSE")

    def tool_specs(self) -> tuple[Any, ...]:
        """awx_common ToolSpecs rebuilt from data (their own validation runs again)."""
        from awx_common.tools import ToolKind, ToolSpec

        return tuple(
            ToolSpec(t.name, ToolKind(t.kind), t.description, t.input_schema) for t in self.tools
        )

    def function_specs(self) -> tuple[Any, ...]:
        from awx_common.functions import KitFunctionSpec, KitGrant

        out = []
        for f in self.functions:
            spec = KitFunctionSpec(
                name=f.name,
                handler=f.handler,
                grants=tuple(KitGrant(g) for g in f.grants),
                timeout_s=f.timeout_s,
                memory_mb=f.memory_mb,
                description=f.description,
            )
            spec.check_package(self.package)
            out.append(spec)
        return tuple(out)


# --- strict parsing -------------------------------------------------------------------------


def _obj(value: Any, keys: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{where} must be an object")
    extra = set(value) - keys
    missing = keys - set(value)
    if extra:
        raise ManifestError(f"{where} has unknown keys {sorted(extra)}")
    if missing:
        raise ManifestError(f"{where} lacks keys {sorted(missing)}")
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where} must be a non-empty string")
    return value


def _int(value: Any, where: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ManifestError(f"{where} must be an int in {lo}..{hi}")
    return value


def _strs(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ManifestError(f"{where} must be a list of strings")
    return tuple(value)


def _check_schema(node: Any, where: str) -> None:
    """Gateway-accepted types only; arrays carry items."""
    if not isinstance(node, dict):
        raise ManifestError(f"{where} must be a JSON Schema object")
    kind = node.get("type")
    kinds = [k for k in kind if k != "null"] if isinstance(kind, list) else [kind]
    if len(kinds) != 1 or kinds[0] not in _GATEWAY_TYPES:
        raise ManifestError(f"{where} type {kind!r} is not a Gateway type")
    if kinds[0] == "array":
        if "items" not in node:
            raise ManifestError(f"{where} is an array without items")
        _check_schema(node["items"], f"{where}.items")
    for name, sub in (node.get("properties") or {}).items():
        _check_schema(sub, f"{where}.{name}")


def _workflow(data: Any, kit: str, where: str, depth: int = 0) -> WorkflowManifest:
    if depth > 3:
        raise ManifestError(f"{where} nests children too deeply")
    d = _obj(
        data,
        {
            "kit",
            "action",
            "approval_ttl_s",
            "steps",
            "read_allowlist",
            "platform_read_allowlist",
            "fan_out",
            "child",
        },
        where,
    )
    if d["kit"] != kit:
        raise ManifestError(f"{where} belongs to kit {d['kit']!r}, not {kit}")
    steps = []
    if not isinstance(d["steps"], list):
        raise ManifestError(f"{where}.steps must be a list")
    for i, s in enumerate(d["steps"]):
        s = _obj(s, {"name", "finality"}, f"{where}.steps[{i}]")
        fin = None
        if s["finality"] is not None:
            f = _obj(s["finality"], {"timeout_s", "interval_s"}, f"{where}.steps[{i}].finality")
            fin = FinalityManifest(
                _int(f["timeout_s"], "finality.timeout_s", 1, 86_400),
                _int(f["interval_s"], "finality.interval_s", 1, 86_400),
            )
            try:
                check_finality_bounds(fin.interval_s, fin.timeout_s)  # FinalitySpec's rule
            except ValueError as exc:
                raise ManifestError(f"{where}.steps[{i}].finality: {exc}") from None
        steps.append(StepManifest(_str(s["name"], f"{where}.steps[{i}].name"), fin))
    child = None
    if d["child"] is not None:
        child = _workflow(d["child"], kit, f"{where}.child", depth + 1)
    if not isinstance(d["fan_out"], bool):
        raise ManifestError(f"{where}.fan_out must be a bool")
    action = _str(d["action"], f"{where}.action")
    ttl_s = _int(d["approval_ttl_s"], f"{where}.approval_ttl_s", 1, 31 * 86_400)
    read_allowlist = _strs(d["read_allowlist"], f"{where}.read_allowlist")
    platform_read_allowlist = _strs(
        d["platform_read_allowlist"], f"{where}.platform_read_allowlist"
    )
    # KitWorkflowSpec's own rules (awx_common.spec_rules), re-run on the loader's untrusted
    # output, so the spec and the manifest cannot drift.
    try:
        check_workflow_shape(
            action=action,
            step_names=[s.name for s in steps],
            has_fan_out=d["fan_out"],
            has_child=child is not None,
            has_child_tenant=False,  # not carried; the kit's own spec checks it at import
            approval_ttl_s=ttl_s,
            read_allowlist=read_allowlist,
            platform_read_allowlist=platform_read_allowlist,
        )
    except ValueError as exc:
        raise ManifestError(f"{where}: {exc}") from None
    return WorkflowManifest(
        kit=kit,
        action=action,
        approval_ttl=timedelta(seconds=ttl_s),
        steps=tuple(steps),
        read_allowlist=read_allowlist,
        platform_read_allowlist=platform_read_allowlist,
        child=child,
    )


def kit_package(kit: str) -> str:
    """The one package a kit may be: derived from KIT_DIRS, never read from a manifest."""
    from awx_common.kits import KIT_DIRS

    if kit not in KIT_DIRS:
        raise ManifestError(f"unknown kit {kit!r}")
    return "awx_" + KIT_DIRS[kit].rsplit("/", 1)[1]


def parse_manifest(
    data: Any, *, expected_kit: str | None = None, stand_in_package: str | None = None
) -> KitManifest:
    """Validate a manifest JSON object strictly and return the typed manifest.

    The loader's output is untrusted. The package is derived from KIT_DIRS for the kit and
    the manifest's `package` must equal it; `expected_kit` (the kit the loader was asked for)
    must equal the manifest's kit. `stand_in_package` exists only for tests that describe an
    in-process stand-in kit (build_manifest); build_app's loader path never passes it."""
    from awx_common.kits import KITS

    d = _obj(
        data, {"schema", "kit", "package", "workflows", "tools", "reads", "functions"}, "manifest"
    )
    if d["schema"] != SCHEMA_VERSION:
        raise ManifestError(f"manifest schema {d['schema']!r} is not {SCHEMA_VERSION}")
    kit = _str(d["kit"], "manifest.kit")
    if kit not in KITS:
        raise ManifestError(f"unknown kit {kit!r}")
    if expected_kit is not None and kit != expected_kit:
        raise ManifestError(f"the {expected_kit} loader returned a manifest for {kit}")
    package = stand_in_package or kit_package(kit)
    if d["package"] != package:
        raise ManifestError(f"manifest for {kit} names package {d['package']!r}, not {package}")
    if not isinstance(d["workflows"], dict) or not d["workflows"]:
        raise ManifestError("manifest.workflows must be a non-empty object")
    workflows = {}
    for action, wf in d["workflows"].items():
        w = _workflow(wf, kit, f"workflows.{action}")
        if w.action != action:
            raise ManifestError(f"workflows.{action} names action {w.action!r}")
        workflows[action] = w
    if not isinstance(d["tools"], list) or not d["tools"]:
        raise ManifestError("manifest.tools must be a non-empty list")
    tools = []
    for i, t in enumerate(d["tools"]):
        t = _obj(t, {"name", "kind", "description", "input_schema"}, f"tools[{i}]")
        name = _str(t["name"], f"tools[{i}].name")
        if t["kind"] not in ("READ", "PROPOSE"):
            raise ManifestError(f"tools[{i}].kind must be READ or PROPOSE")
        if len(kit) + 3 + len(name) > 64:
            raise ManifestError(f"tool {name!r} is too long once prefixed by {kit}___")
        _check_schema(t["input_schema"], f"tools.{name}.input_schema")
        tools.append(
            ToolManifest(
                name, t["kind"], _str(t["description"], f"{name}.description"), t["input_schema"]
            )
        )
    names = [t.name for t in tools]
    if len(set(names)) != len(names):
        raise ManifestError("a tool name appears twice")
    reads = _strs(d["reads"], "manifest.reads")
    for t in tools:
        if t.kind == "PROPOSE" and t.name.removeprefix("propose_") not in workflows:
            raise ManifestError(f"propose tool {t.name!r} has no SPECS entry")
        if t.kind == "READ" and t.name not in reads:
            raise ManifestError(f"read tool {t.name!r} has no READS entry")
    if not isinstance(d["functions"], list):
        raise ManifestError("manifest.functions must be a list")
    functions = []
    for i, f in enumerate(d["functions"]):
        f = _obj(
            f,
            {"name", "handler", "grants", "timeout_s", "memory_mb", "description"},
            f"functions[{i}]",
        )
        if not isinstance(f["description"], str):
            raise ManifestError(f"functions[{i}].description must be a string")
        functions.append(
            FunctionManifest(
                _str(f["name"], f"functions[{i}].name"),
                _str(f["handler"], f"functions[{i}].handler"),
                _strs(f["grants"], f"functions[{i}].grants") if f["grants"] else (),
                _int(f["timeout_s"], f"functions[{i}].timeout_s", 1, 300),
                _int(f["memory_mb"], f"functions[{i}].memory_mb", 128, 1024),
                f["description"],
            )
        )
    manifest = KitManifest(
        kit,
        package,
        workflows,
        tuple(tools),
        reads,
        tuple(functions),
        stand_in=stand_in_package is not None,
    )
    manifest.tool_specs()  # ToolSpec's own checks (names, server-owned fields)
    manifest.function_specs()  # KitFunctionSpec's checks, grant menu, handler package
    return manifest


# --- building a manifest (runs where kit code may run: the loader subprocess, or a test) ----


def workflow_manifest(spec: Any) -> dict[str, Any]:
    """JSON for one KitWorkflowSpec (and its child)."""
    return {
        "kit": spec.kit,
        "action": spec.action,
        "approval_ttl_s": int(spec.approval_ttl.total_seconds()),
        "steps": [
            {
                "name": s.name,
                "finality": None
                if s.finality is None
                else {
                    "timeout_s": int(s.finality.timeout_s),
                    "interval_s": int(s.finality.interval_s),
                },
            }
            for s in spec.steps
        ],
        "read_allowlist": list(spec.read_allowlist),
        "platform_read_allowlist": list(spec.platform_read_allowlist),
        "fan_out": spec.fan_out is not None,
        "child": None if spec.child is None else workflow_manifest(spec.child),
    }


def parse_workflow(spec: Any) -> WorkflowManifest:
    """A stand-in spec (tests only) as the manifest the CDK code takes, via the same JSON."""
    return _workflow(json.loads(json.dumps(workflow_manifest(spec))), spec.kit, spec.action)


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def build_manifest_json(kit: str, package: str | None = None) -> dict[str, Any]:
    """Import the kit and describe it. Call only in the loader subprocess or in a test."""
    from awx_agent.registry import load_kit_tools
    from awx_common.functions import KitFunctionSpec

    package = package or kit_package(kit)
    pkg = importlib.import_module(package)
    if _module_exists(f"{package}.infra") or hasattr(pkg, "extend"):
        raise RemovedExtensionHook(
            f"{package} ships `infra`/`extend`, which is not supported: a kit "
            f"writes no CDK. Declare functions in {package}.functions.FUNCTIONS "
            "(awx_common.functions.KitFunctionSpec) and KitStack builds them."
        )
    plan = load_kit_tools(kit, f"{package}.tools")  # TOOLS/SPECS/READS as the Lambda checks
    functions = []
    if _module_exists(f"{package}.functions"):
        declared = getattr(importlib.import_module(f"{package}.functions"), "FUNCTIONS", None)
        if declared is None:
            raise ManifestError(f"{package}.functions must export FUNCTIONS")
        for f in tuple(declared):
            if not isinstance(f, KitFunctionSpec):
                raise ManifestError(f"{package}.functions.FUNCTIONS must hold KitFunctionSpec")
            f.check_package(package)
            module = f.handler.rsplit(".", 1)[0]
            if not _module_exists(module):
                raise ManifestError(f"handler module {module} does not exist")
            functions.append(
                {
                    "name": f.name,
                    "handler": f.handler,
                    "grants": [g.value for g in f.grants],
                    "timeout_s": f.timeout_s,
                    "memory_mb": f.memory_mb,
                    "description": f.description,
                }
            )
    names = [f["name"] for f in functions]
    if len(set(names)) != len(names):
        raise ManifestError(f"{package}.functions declares a name twice")
    return {
        "schema": SCHEMA_VERSION,
        "kit": kit,
        "package": package,
        "workflows": {a: workflow_manifest(s) for a, s in sorted(plan.specs.items())},
        "tools": [
            {
                "name": t.name,
                "kind": t.kind.value,
                "description": t.description,
                "input_schema": json.loads(json.dumps(t.input_schema)),
            }
            for t in plan.tools
        ],
        "reads": sorted(plan.reads),
        "functions": functions,
    }


def build_manifest(kit: str, package: str | None = None) -> KitManifest:
    """In-process manifest, for TESTS with a stand-in kit package only. build_app never calls
    this for a real kit; it uses load_manifests (the subprocess)."""
    return parse_manifest(
        json.loads(json.dumps(build_manifest_json(kit, package))),
        expected_kit=kit,
        stand_in_package=package,
    )


# --- loading from the subprocess (the only path build_app uses for real kits) --------------

_CACHE: dict[tuple[str, tuple[str, ...]], KitManifest] = {}


def _run_loader(kit: str, pythonpath: Sequence[str]) -> dict[str, Any]:
    env = dict(os.environ)
    paths = [*pythonpath, str(REPO_ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    proc = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a kit id
        [sys.executable, "-m", "infra.kit_manifest", kit],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
        raise ManifestError(f"the {kit} manifest loader failed: {tail}")
    return json.loads(proc.stdout)


def load_manifests(
    kits: Iterable[str], *, pythonpath: Sequence[str] = (), cache: bool = True
) -> dict[str, KitManifest]:
    """One loader subprocess per kit (in parallel), each manifest validated. Cached per
    process by (kit, pythonpath), so a test session loads each real kit once."""
    kits = list(kits)
    key_path = tuple(pythonpath)
    todo = [k for k in kits if not (cache and (k, key_path) in _CACHE)]
    fresh: dict[str, KitManifest] = {}
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(todo)) as pool:
            raw = dict(zip(todo, pool.map(lambda k: _run_loader(k, key_path), todo), strict=True))
        for k in todo:
            manifest = parse_manifest(raw[k], expected_kit=k)
            fresh[k] = manifest
            if cache:
                _CACHE[(k, key_path)] = manifest
    return {k: fresh[k] if k in fresh else _CACHE[(k, key_path)] for k in kits}


def main(argv: Sequence[str]) -> int:
    (kit,) = argv
    out = sys.stdout
    sys.stdout = sys.stderr  # anything the kit prints at import cannot corrupt the manifest
    try:
        data = build_manifest_json(kit)
    finally:
        sys.stdout = out
    parse_manifest(json.loads(json.dumps(data)))  # validate before printing
    sys.stdout.write(json.dumps(data, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
