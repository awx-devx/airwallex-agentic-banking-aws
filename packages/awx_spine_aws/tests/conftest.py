"""Test setup for awx_spine_aws. The suites run against the real awx_spine package."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # so tests can `import toy`

from aws_harness import aws, testkit  # noqa: E402, F401  shared fixtures
from test_asl_equivalence import machines  # noqa: E402, F401  the synthesised ASL fixture


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    """Fake region and credentials so boto3 clients build; moto intercepts every call."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
