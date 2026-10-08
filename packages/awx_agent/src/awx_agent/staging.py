"""Stage Lambda and AgentCore Runtime code assets at synth time.

Both are direct code deployment (zip), not containers. The tool and interceptor Lambdas need
first-party source, boto3 (provided by the Lambda Python 3.12 runtime) and the token verifier's
wheels (pyjwt, cryptography and cryptography's cffi/pycparser), pinned to uv.lock and installed
as linux/arm64 CPython 3.12 binary wheels into a per-pin cache that every Lambda asset copies.
The Runtime asset also needs strands-agents and bedrock-agentcore (and the same verifier wheels);
all are installed as linux/arm64 CPython 3.12 wheels with `uv pip install --python-platform`, which
is cross-platform by construction and needs no Docker or emulation.

Set the environment variable AWX_SKIP_RUNTIME_DEPS=1 (the synth tests do) to stage the Runtime
asset from source only, which keeps synth offline and fast.
"""

from __future__ import annotations

import atexit
import hashlib
import importlib.util
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent  # packages/awx_agent/src/awx_agent
REPO_ROOT = _PKG_DIR.parents[3]
# Only the pinned wheel cache is shared between processes (it is written atomically). Every
# staged asset directory lives in a per-process run directory (run_dir), so concurrent pytest
# or cdk runs in the same checkout cannot rmtree or rewrite each other's assets mid-hash.
STAGING_ROOT = REPO_ROOT / "packages" / "awx_agent" / ".staging"
_RUN_DIRS: dict[tuple[int, str], Path] = {}


def run_dir(kind: str) -> Path:
    """This process's private staging directory for `kind`, created on first use and removed
    at exit. Under $AWX_STAGING_DIR if set, else the system temp directory. CDK copies each
    asset into the cloud assembly when the construct is created, so nothing reads it later."""
    key = (os.getpid(), kind)  # a forked child gets its own
    if key not in _RUN_DIRS:
        base = os.environ.get("AWX_STAGING_DIR") or None
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        d = Path(tempfile.mkdtemp(prefix=f"awx-{kind}-", dir=base))
        atexit.register(shutil.rmtree, d, True)
        _RUN_DIRS[key] = d
    return _RUN_DIRS[key]


_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".pytest_cache")
# The Runtime never runs the Gateway Lambda handlers (they import the spine engine, which the
# Runtime asset does not carry), so leave them out; test_synth checks every first-party import
# in the Runtime asset resolves inside it.
_RUNTIME_EXCLUDED = ("handlers", "testing.py")  # awx_agent/handlers/, awx_agent/testing.py


def _RUNTIME_IGNORE(directory: str, names: list[str]) -> set[str]:
    """Like _IGNORE, plus the two awx_agent entries the Runtime must not carry. Anchored to
    the awx_agent package directory, so another package's testing.py or handlers/ is kept."""
    ignored = set(_IGNORE(directory, names))
    if Path(directory).resolve() == _PKG_DIR:
        ignored |= {n for n in names if n in _RUNTIME_EXCLUDED}
    return ignored


RUNTIME_ENTRY = "main.py"
_RUNTIME_MAIN = '''"""AgentCore Runtime entrypoint: serve awx_agent.runtime.app on port 8080."""

from awx_agent.runtime.app import app

if __name__ == "__main__":
    app.run()
'''


def package_dir(module: str) -> Path:
    """The top-level package directory that holds `module` (e.g. .../src/awx_k1_treasury)."""
    top = module.split(".", 1)[0]
    spec = importlib.util.find_spec(top)
    if spec is None or not spec.submodule_search_locations:
        raise ModuleNotFoundError(f"cannot locate package {top!r}")
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def first_party_dirs() -> list[Path]:
    return [package_dir("awx_common"), package_dir("awx_agent")]


def kit_source_dirs(tools_module: str) -> list[Path]:
    """Tool Lambda source: the agent plane, the spine engine it previews with (awx_spine_aws,
    which imports awx_spine at module top), and the kit. test_synth imports each staged
    handler in isolation, so a missing first-party package fails CI, not the deploy."""
    return [
        *first_party_dirs(),
        package_dir("awx_spine"),
        package_dir("awx_spine_aws"),
        package_dir(tools_module),
    ]


def _reset(name: str) -> Path:
    out = run_dir("agent") / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    return out


LAMBDA_WHEELS = ("pyjwt", "cryptography", "cffi", "pycparser")
_PLATFORM = ("--python-platform", "aarch64-manylinux2014", "--python-version", "3.12")


def locked_pins(names: Sequence[str]) -> list[str]:
    """name==version for each package, from uv.lock (so assets match what the tests ran)."""
    import tomllib

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    versions = {p["name"]: p["version"] for p in lock["package"]}
    missing = [n for n in names if n not in versions]
    if missing:
        raise LookupError(f"not in uv.lock: {missing}")
    return [f"{n}=={versions[n]}" for n in names]


def _lambda_wheels() -> Path:
    pins = locked_pins(LAMBDA_WHEELS)
    key = hashlib.sha256("\n".join([*pins, *_PLATFORM]).encode()).hexdigest()[:16]
    cache = STAGING_ROOT / f"_wheels-{key}"
    if (cache / ".complete").exists():
        return cache
    # Install into a private sibling and rename it into place, so a concurrent run never sees
    # (or deletes) a half-written cache.
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".wheels-{key}-", dir=STAGING_ROOT))
    try:
        _install_wheels(tmp, pins)
        (tmp / ".complete").write_text("\n".join(pins))
        if cache.exists() and not (cache / ".complete").exists():
            shutil.rmtree(cache, ignore_errors=True)  # an old, interrupted non-atomic cache
        try:
            tmp.rename(cache)
        except OSError:
            if not (cache / ".complete").exists():  # lost the race to a complete cache: fine
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return cache


def _install_wheels(target: Path, pins: Sequence[str]) -> None:
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            _uv(),
            "--color",
            "never",  # a CLI parent (npx cdk) may force colour; requirements must be plain
            "pip",
            "install",
            "--target",
            str(target),
            *_PLATFORM,
            "--only-binary",
            ":all:",
            "--no-deps",
            *pins,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )


def stage_lambda_source(name: str, dirs: Sequence[Path]) -> Path:
    out = _reset(name)
    shutil.copytree(
        _lambda_wheels(), out, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".complete")
    )
    for d in dirs:
        shutil.copytree(d, out / d.name, ignore=_IGNORE)
    return out


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError("uv is needed to stage the AgentCore Runtime asset")
    return uv


def _runtime_requirements() -> str:
    """Third-party pins for the Runtime, exported from uv.lock so they match the tests."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            _uv(),
            "--color",
            "never",  # a CLI parent (npx cdk) may force colour; requirements must be plain
            "export",
            "--package",
            "awx-agent",
            "--no-hashes",
            "--no-emit-workspace",
            "--no-dev",
            "--frozen",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def stage_runtime(name: str = "runtime") -> Path:
    out = _reset(name)
    for d in first_party_dirs():
        shutil.copytree(d, out / d.name, ignore=_RUNTIME_IGNORE)
    (out / RUNTIME_ENTRY).write_text(_RUNTIME_MAIN)
    if os.environ.get("AWX_SKIP_RUNTIME_DEPS") == "1":
        return out
    req = out.parent / f"{name}-requirements.txt"
    req.write_text(_runtime_requirements())
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            _uv(),
            "--color",
            "never",  # a CLI parent (npx cdk) may force colour; requirements must be plain
            "pip",
            "install",
            "--target",
            str(out),
            "--python-platform",
            "aarch64-manylinux2014",
            "--python-version",
            "3.12",
            "--only-binary",
            ":all:",
            "-r",
            str(req),
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    return out
