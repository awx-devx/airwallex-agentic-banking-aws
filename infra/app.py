"""CDK entry point: `npx -y aws-cdk@2.1144.0 synth` from infra/ (cdk.json runs this module).

The account comes from `-c account=<id>` or CDK_DEFAULT_ACCOUNT, the Region from
`-c region=<region>` (default awx_common.regions.DEFAULT_REGION), the agent's model IDs from
`-c model_id=<id>` and `-c fallback_model_id=<id>` (default: the Region's inference profiles,
awx_common.regions.default_model_ids), and the alarm and budget email from
`-c alert_email=<address>` or AWX_ALERT_EMAIL. Synth makes no AWS calls and no context
lookups, so it needs no credentials. cdk-nag AwsSolutions runs over every stack.
"""

from __future__ import annotations

import os
import re
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
from awx_common.regions import (  # noqa: E402
    DEFAULT_REGION,
    INFERENCE_PROFILE_PREFIX,
    UnsupportedRegionError,
    default_model_ids,
)
from cdk_nag import AwsSolutionsChecks  # noqa: E402

from infra.kit_manifest import KitManifest, load_manifests  # noqa: E402
from infra.stacks.agent_stack import AgentStack  # noqa: E402
from infra.stacks.kit_stack import KitStack  # noqa: E402
from infra.stacks.spine_stack import SpineStack  # noqa: E402


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


REGION_CODE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")


def target_region(app: cdk.App) -> str:
    """The Region to build for: `-c region=<region>`, else DEFAULT_REGION when the context is
    absent. A present but empty value (REGION=$REGION with REGION unset) is refused, never
    read as the default, and so is anything that is not a Region code."""
    value = app.node.try_get_context("region")
    if value is None:
        return DEFAULT_REGION
    region = str(value).strip()
    if not region:
        raise ValueError(
            "REGION is empty (-c region= has no value). Set it to the Region to deploy to, "
            "for example: export REGION=us-west-2"
        )
    if not REGION_CODE.match(region):
        raise ValueError(f"REGION={region} is not an AWS Region code such as us-west-2")
    return region


def _model_id_context(app: cdk.App, key: str, variable: str) -> str | None:
    value = str(app.node.try_get_context(key) or "").strip()
    if value.startswith("arn:"):
        raise ValueError(
            f"{variable}={value} is an ARN. Pass a model ID or an inference profile ID, "
            "for example us.anthropic.claude-sonnet-5; ARNs are not supported."
        )
    return value or None


def target_model_ids(app: cdk.App, region: str) -> tuple[str, str]:
    """(primary, fallback) model IDs: `-c model_id` and `-c fallback_model_id`, each falling
    back to the Region's default inference profile. A Region with no defaults needs
    `model_id` and `fallback_model_id`."""
    primary = _model_id_context(app, "model_id", "MODEL_ID")
    fallback = _model_id_context(app, "fallback_model_id", "FALLBACK_MODEL_ID")
    if primary and fallback:
        return primary, fallback
    try:
        default_primary, default_fallback = default_model_ids(region)
    except UnsupportedRegionError:
        missing = [
            f"{variable} (CDK context {key})"
            for variable, key, given in (
                ("MODEL_ID", "model_id", primary),
                ("FALLBACK_MODEL_ID", "fallback_model_id", fallback),
            )
            if not given
        ]
        raise ValueError(
            f"REGION={region} has no default model IDs (supported: "
            f"{', '.join(sorted(INFERENCE_PROFILE_PREFIX))}). Pass {' and '.join(missing)} "
            "with model or inference profile IDs that this Region offers."
        ) from None
    return primary or default_primary, fallback or default_fallback


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
    region = target_region(app)
    model_ids = target_model_ids(app, region)
    env = cdk.Environment(account=target_account(app), region=region)
    selected = list(KIT_DIRS if kits is None else kits)
    given = dict(manifests or {})
    loaded = load_manifests([k for k in selected if k not in given])
    built_manifests = {k: given.get(k) or loaded[k] for k in selected}
    spine = SpineStack(app, "AwxSpine", env=env)
    agent = AgentStack(app, "AwxAgentStack", spine=spine, model_ids=model_ids, env=env)
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
