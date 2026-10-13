"""Test-session defaults for every package.

Synth tests build the CDK app, which needs a target account and an alert email
(infra/app.py, infra/stacks/spine_stack.py). Tests use the AWS documentation placeholder
account and an example.com address, so no real account or mailbox appears in a template or
an assertion. A value already set in the environment wins.
"""

from __future__ import annotations

import os

from awx_common.regions import DEFAULT_REGION

TEST_ACCOUNT = "111122223333"
TEST_REGION = DEFAULT_REGION
TEST_ALERT_EMAIL = "alerts@example.com"

os.environ.setdefault("CDK_DEFAULT_ACCOUNT", TEST_ACCOUNT)
os.environ.setdefault("AWX_ALERT_EMAIL", TEST_ALERT_EMAIL)
