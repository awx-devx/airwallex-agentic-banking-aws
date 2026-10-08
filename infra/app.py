"""CDK entry point: `npx -y aws-cdk@2.1144.0 synth` from infra/ (cdk.json runs this module).

The account comes from `-c account=<id>` or CDK_DEFAULT_ACCOUNT, the region from
`-c region=<region>` (default ap-southeast-2), and the alarm and budget email from
`-c alert_email=<address>` or AWX_ALERT_EMAIL. Synth makes no AWS calls and no context
lookups, so it needs no credentials. cdk-nag AwsSolutions runs over every stack.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

# Project modules are imported as infra.* with the repo root on sys.path. infra/constructs is
# a namespace package (no __init__.py) so it can never shadow the jsii `constructs` library,
# and infra/ itself is taken off sys.path for the same reason when run as a script.
_INFRA = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _INFRA]
if str(_INFRA.parent) not in sys.path:
    sys.path.insert(0, str(_INFRA.parent))

import aws_cdk as cdk  # noqa: E402
from awx_common.kits import KIT_DIRS  # noqa: E402
from cdk_nag import AwsSolutionsChecks  # noqa: E402

from infra.kit_manifest import KitManifest, load_manifests  # noqa: E402
from infra.stacks.agent_stack import AgentStack  # noqa: E402
from infra.stacks.kit_stack import KitStack  # noqa: E402
from infra.stacks.spine_stack import SpineStack  # noqa: E402

DEFAULT_REGION = "ap-southeast-2"


def target_account(app: cdk.App) -> str:
    """The account to build for: `-c account=<id>`, else CDK_DEFAULT_ACCOUNT (the CDK CLI sets
    it from the active credentials). There is no built-in default, so synth never targets an
    account by accident."""
    account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")
    if not account:
        raise ValueError(
            "no target account: pass -c account=<12-digit id> or set CDK_DEFAULT_ACCOUNT"
        )
    return str(account)


def build_app(
    app: cdk.App | None = None,
    *,
    kits: Iterable[str] | None = None,
    manifests: Mapping[str, KitManifest] | None = None,
) -> cdk.App:
    """The deployable app: spine, one KitStack per kit, agent stack.

    Kits: every kit in KIT_DIRS, unless `kits` names a subset (tests that register their own
    stand-in kit pass kits=()) or the `kits` context does (`cdk synth -c kits=k1,k4`, for a
    partial deploy). A partial agent stack carries only those kits' Gateway targets,
    so deploying it removes every other kit's tools; synth warns about it.

    Kit code never runs in this process: each kit's KitManifest comes
    from infra.kit_manifest's loader subprocess. Tests pass `manifests` directly for kits they
    stand in for; any kit not in `manifests` is loaded.
    """
    app = app or cdk.App()
    context = app.node.try_get_context("kits")
    if kits is None and context is not None:
        kits = [k.strip() for k in str(context).split(",") if k.strip()]
        if not kits:
            raise ValueError("-c kits names no kit; omit it to build every kit")
        unknown = sorted(set(kits) - set(KIT_DIRS))
        if unknown:
            raise ValueError(f"-c kits names unknown kits {unknown}")
    env = cdk.Environment(
        account=target_account(app),
        region=app.node.try_get_context("region") or DEFAULT_REGION,
    )
    selected = list(KIT_DIRS if kits is None else kits)
    given = dict(manifests or {})
    loaded = load_manifests([k for k in selected if k not in given])
    built_manifests = {k: given.get(k) or loaded[k] for k in selected}
    spine = SpineStack(app, "AwxSpine", env=env)
    agent = AgentStack(app, "AwxAgentStack", spine=spine, env=env)
    # spine <- kit stacks <- agent stack: each kit's tools live in the agent stack and point
    # at the kit's state machines; no kit stack references the agent stack.
    for kit in selected:
        KitStack(
            app,
            f"AwxKit{kit.upper()}",
            spine=spine,
            agent=agent,
            kit=kit,
            manifest=built_manifests[kit],
            env=env,
        )
    built = set(KIT_DIRS if kits is None else kits)
    if context is not None and built != set(KIT_DIRS):
        cdk.Annotations.of(agent).add_warning_v2(
            "awx:partial-agent-stack",
            f"AwxAgentStack carries Gateway targets for {sorted(built)} only; deploying it "
            f"removes the tools of {sorted(set(KIT_DIRS) - built)} (infra/README.md).",
        )
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    return app


if __name__ == "__main__":
    build_app().synth()
