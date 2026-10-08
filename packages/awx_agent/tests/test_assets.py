"""Every Lambda asset the repo stages imports in isolation; the Runtime asset's first-party
imports all resolve inside it.

The unit and synth tests run inside the uv venv, where every workspace package is installed,
so an asset that forgot a first-party package (for example a tool asset without awx_spine)
still passes them. These tests import each synthesized asset's handler in a
`python -S` subprocess whose sys.path is only the asset directory plus what the Lambda Python
runtime itself provides (boto3, botocore and their dependencies).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.synth

# What the AWS Lambda Python 3.12 runtime puts on sys.path besides the function's code.
LAMBDA_RUNTIME_MODULES = (
    "boto3",
    "botocore",
    "jmespath",
    "dateutil",
    "six",
    "urllib3",
    "s3transfer",
)
FIRST_PARTY_PREFIXES = ("awx_",)


@pytest.fixture(scope="module")
def assembly():
    """The deployable app exactly as infra/app.py builds it: spine, all eight kit stacks,
    agent stack."""
    import os

    os.environ["AWX_SKIP_RUNTIME_DEPS"] = "1"
    from aws_cdk import App

    from infra.app import build_app

    return build_app(App()).synth()


@pytest.fixture(scope="module")
def lambda_runtime_dir(tmp_path_factory) -> Path:
    """A directory holding only what the Lambda runtime provides (copied from the venv)."""
    out = tmp_path_factory.mktemp("lambda-runtime")
    for name in LAMBDA_RUNTIME_MODULES:
        spec = importlib.util.find_spec(name)
        assert spec is not None, name
        assert spec.origin, name
        if spec.submodule_search_locations:
            src = Path(next(iter(spec.submodule_search_locations)))
            shutil.copytree(src, out / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(spec.origin, out / Path(spec.origin).name)
    return out


def _functions(assembly):
    """(stack, logical id, handler module, asset dir, environment) for every Lambda."""
    for stack in assembly.stacks:
        out = Path(assembly.directory)
        for lid, res in stack.template["Resources"].items():
            if res["Type"] != "AWS::Lambda::Function":
                continue
            props = res["Properties"]
            code = props.get("Code", {})
            if "S3Key" not in code:
                continue  # inline code (none in our stacks) or custom-resource providers
            asset = out / f"asset.{code['S3Key'].removesuffix('.zip')}"
            if not (asset / "awx_common").exists() and not any(asset.glob("awx_*")):
                continue  # CDK's own helper functions (log retention etc.)
            module = props["Handler"].rsplit(".", 1)[0]
            env = (props.get("Environment") or {}).get("Variables") or {}
            yield stack.stack_name, lid, module, asset, env


def _import_in_isolation(
    asset: Path, runtime: Path, modules: list[str]
) -> subprocess.CompletedProcess:
    code = (
        "import sys, importlib\n"
        f"sys.path[:0] = [{str(asset)!r}, {str(runtime)!r}]\n"
        f"for m in {modules!r}:\n"
        "    importlib.import_module(m)\n"
        "print('ok')\n"
    )
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AWS_DEFAULT_REGION": "ap-southeast-2"},
        cwd=str(asset),
        check=False,
        timeout=120,
    )


def _sibling(module: str, leaf: str) -> str:
    return f"{module.rpartition('.')[0]}.{leaf}"


def test_every_staged_lambda_asset_imports_its_handler_in_isolation(assembly, lambda_runtime_dir):
    if platform.machine() not in ("aarch64", "arm64"):
        pytest.skip(
            f"assets carry linux/arm64 wheels (cryptography); cannot import them on "
            f"{platform.machine()}"
        )
    seen = set()
    kinds = set()  # (kit, "step" | "tools") covered
    failures = []
    for stack, lid, module, asset, env in _functions(assembly):
        modules = [module]
        if "AWX_TOOLS_MODULE" in env:  # tool Lambda: the kit modules load_kit_tools imports
            kinds.add((env["AWX_KIT"], "tools"))
            tools = env["AWX_TOOLS_MODULE"]
            modules += [tools, _sibling(tools, "workflow"), _sibling(tools, "reads")]
        if "AWX_SPEC_MODULE" in env:  # kit-step Lambda: the spec module root_spec imports
            modules.append(env["AWX_SPEC_MODULE"])
            kinds.add(("k" + env["AWX_SPEC_MODULE"].split("_")[1][1:], "step"))
        seen.add(module)
        res = _import_in_isolation(asset, lambda_runtime_dir, modules)
        if res.returncode != 0 or "ok" not in res.stdout:
            failures.append(f"{stack}/{lid} ({module}): {res.stderr.strip().splitlines()[-1:]}")
    assert not failures, failures
    expected = {
        "awx_agent.handlers.tools",
        "awx_agent.handlers.interceptor",
        "awx_spine_aws.handlers.reader",
        "awx_spine_aws.handlers.adapter",
        "awx_spine_aws.handlers.approval",
        "awx_spine_aws.handlers.kit_step",
    }
    assert expected <= seen, expected - seen
    from awx_common.kits import KITS

    assert {k for k, kind in kinds if kind == "step"} == set(KITS), kinds
    assert {k for k, kind in kinds if kind == "tools"} == set(KITS), kinds


def test_isolated_import_catches_a_missing_first_party_package(
    assembly, lambda_runtime_dir, tmp_path
):
    """The check is real: the tool asset without awx_spine fails exactly as review r3 saw."""
    if platform.machine() not in ("aarch64", "arm64"):
        pytest.skip("assets carry linux/arm64 wheels")
    tool = next(
        f
        for f in _functions(assembly)
        if f[2] == "awx_agent.handlers.tools" and f[4].get("AWX_KIT") == "k1"
    )
    broken = tmp_path / "tools-broken"
    shutil.copytree(tool[3], broken, ignore=shutil.ignore_patterns("awx_spine"))
    res = _import_in_isolation(broken, lambda_runtime_dir, ["awx_agent.handlers.tools"])
    assert res.returncode != 0
    assert "No module named 'awx_spine'" in res.stderr


def _package_of(rel: Path) -> list[str]:
    """Dotted package of a file under the asset: a/b/c.py -> [a, b]; a/b/__init__.py -> [a, b]."""
    parts = list(rel.with_suffix("").parts)
    return parts[:-1]


def _module_targets(tree: ast.AST, rel: Path) -> list[tuple[str, str | None]]:
    """(dotted module, optional imported name) for every first-party import in a file.

    Relative imports resolve against the file's package. For `from X import y`, y may be a
    submodule or an attribute of X; the caller accepts either."""
    out: list[tuple[str, str | None]] = []
    package = _package_of(rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, None) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                if node.level - 1 > len(package):
                    out.append(("<relative import above the asset root>", None))
                    continue
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            out += [(module, a.name) for a in node.names]
    return [(m, n) for m, n in out if m.startswith(FIRST_PARTY_PREFIXES) or m.startswith("<")]


def _module_exists(root: Path, dotted: str) -> bool:
    path = root.joinpath(*dotted.split("."))
    return (path / "__init__.py").is_file() or path.with_suffix(".py").is_file()


def _source_has_submodule(module: str, name: str) -> bool:
    """Whether the installed source of package `module` has a submodule `name`."""
    top = module.split(".")[0]
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.submodule_search_locations:
        return False
    base = Path(next(iter(spec.submodule_search_locations))).joinpath(*module.split(".")[1:])
    return (base / f"{name}.py").is_file() or (base / name / "__init__.py").is_file()


def unresolved_first_party_imports(root: Path) -> dict[str, list[str]]:
    """Every awx_* import in the asset's first-party code that names a module the asset lacks."""
    carried = {p.name for p in root.iterdir() if p.is_dir() and p.name.startswith("awx_")}
    missing: dict[str, list[str]] = {}
    for py in root.rglob("*.py"):
        rel = py.relative_to(root)
        if rel.parts[0] not in carried and rel.name != "main.py":
            continue  # third-party wheels (absent in tests) and anything else
        for module, name in _module_targets(ast.parse(py.read_text()), rel):
            if name and _source_has_submodule(module, name):
                # `from X import y` where y is a submodule in the source tree: the asset must
                # carry that submodule, not just X.
                if not _module_exists(root, f"{module}.{name}"):
                    missing.setdefault(f"{module}.{name}", []).append(str(rel))
                continue
            if _module_exists(root, module):
                continue  # a plain import, or `from X import y` with y an attribute of X
            missing.setdefault(module, []).append(str(rel))
    return missing


def _runtime_asset(assembly) -> Path:
    out = Path(assembly.directory)
    (runtime,) = [p.parent for p in out.glob("asset.*/main.py")]
    return runtime


def test_runtime_asset_first_party_imports_all_resolve_inside_it(assembly):
    """The Runtime asset's third-party wheels are skipped in tests (AWX_SKIP_RUNTIME_DEPS), so
    check statically that every awx_* import (full dotted module, relative imports resolved)
    names a module file the asset carries."""
    runtime = _runtime_asset(assembly)
    assert (runtime / "awx_agent" / "runtime" / "app.py").is_file()
    assert not (runtime / "awx_agent" / "handlers").exists()  # deliberately excluded
    missing = unresolved_first_party_imports(runtime)
    assert not missing, json.dumps(missing)[:500]


@pytest.mark.parametrize(
    "line",
    [
        "from awx_agent.handlers import tools\n",
        "import awx_agent.handlers.tools\n",
        "from ..handlers import tools\n",
        "from awx_spine_aws import engine\n",
        "from awx_agent import handlers\n",  # a submodule imported as a name
        "from awx_agent import testing\n",
        "@init:from . import handlers\n",  # inside awx_agent/__init__.py
    ],
)
def test_runtime_import_check_catches_an_excluded_module(assembly, tmp_path, line):
    """An import of an excluded module (any spelling, including `from X import y` with y a
    submodule the asset lacks) must fail."""
    copy = tmp_path / "runtime"
    shutil.copytree(_runtime_asset(assembly), copy)
    if line.startswith("@init:"):
        target, line = copy / "awx_agent" / "__init__.py", line.removeprefix("@init:")
    else:
        target = copy / "awx_agent" / "runtime" / "app.py"
    target.write_text(line + target.read_text())
    missing = unresolved_first_party_imports(copy)
    assert missing, line


def test_spine_lambda_raises_on_a_missing_spine_package(monkeypatch):
    from infra.constructs import spine_lambda

    real = spine_lambda.package_dir
    monkeypatch.setattr(
        spine_lambda, "package_dir", lambda name: None if name == "awx_spine" else real(name)
    )
    try:
        with pytest.raises(ModuleNotFoundError, match="awx_spine"):
            spine_lambda.staged_code("probe-missing-spine", spine_lambda.SPINE_PACKAGES)
    finally:
        shutil.rmtree(spine_lambda.staging_root() / "probe-missing-spine", ignore_errors=True)


def test_staged_code_raises_for_any_missing_package_unless_a_stand_in_is_allowed():
    """Only an explicit allow_stand_in=True skips a missing non-spine package."""
    from infra.constructs import spine_lambda

    try:
        with pytest.raises(ModuleNotFoundError, match="awx_no_such_kit"):
            spine_lambda.staged_code("probe-missing-kit", ("awx_common", "awx_no_such_kit"))
        spine_lambda.staged_code(
            "probe-missing-kit", ("awx_common", "awx_no_such_kit"), allow_stand_in=True
        )
        assert (spine_lambda.staging_root() / "probe-missing-kit" / "awx_common").is_dir()
    finally:
        shutil.rmtree(spine_lambda.staging_root() / "probe-missing-kit", ignore_errors=True)


def test_declared_kit_function_handler_imports_in_isolation(monkeypatch, lambda_runtime_dir):
    """A kit's declared FUNCTIONS get the same isolated-import check."""
    import importlib.machinery
    import types

    from aws_cdk import App
    from awx_common.functions import KitFunctionSpec, KitGrant

    from infra.app import build_app

    if platform.machine() not in ("aarch64", "arm64"):
        pytest.skip("assets carry linux/arm64 wheels")
    name = "awx_k4_dispute.functions"
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    module.FUNCTIONS = (
        KitFunctionSpec(
            "evidence_upload",
            "awx_k4_dispute.evidence.ingest_evidence",
            (KitGrant.EVIDENCE_PUT, KitGrant.KIT_STORE),
        ),
    )
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("AWX_SKIP_RUNTIME_DEPS", "1")
    from infra.kit_manifest import build_manifest

    manifest = build_manifest("k4")  # a stand-in, described in-process (tests only)
    assembly = build_app(App(), kits=("k4",), manifests={"k4": manifest}).synth()
    found = [f for f in _functions(assembly) if "AWX_FUNCTION" in f[4]]
    assert len(found) == 1
    _stack, _lid, module_name, asset, _env = found[0]
    assert module_name == "awx_k4_dispute.evidence"
    res = _import_in_isolation(asset, lambda_runtime_dir, [module_name])
    assert res.returncode == 0, res.stderr[-500:]
