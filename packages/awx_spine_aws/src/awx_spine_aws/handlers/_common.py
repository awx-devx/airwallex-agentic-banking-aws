"""Shared plumbing for the Lambda handlers: configuration, cached clients, safe logging."""

from __future__ import annotations

import functools
import json
import logging
import os
from typing import Any

LOG = logging.getLogger("awx_spine_aws")
LOG.setLevel(logging.INFO)


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is not set")
    return value


@functools.cache
def boto_client(service: str) -> Any:
    import boto3

    return boto3.client(service)


def log(event: str, **fields: Any) -> None:
    """One JSON line. Callers pass identifiers and statuses only: never a body, a header,
    a token or a credential."""
    LOG.info(json.dumps({"event": event, **fields}, default=str, sort_keys=True))


def reset_caches() -> None:
    """For tests: drop cached clients and providers."""
    boto_client.cache_clear()
