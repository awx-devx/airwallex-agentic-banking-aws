"""Kits import the dedupe margin from awx_common, the package they depend on."""

from datetime import timedelta

from awx_common.ops import DEDUP_RESEND_MARGIN


def test_the_dedupe_margin_is_six_days():
    assert timedelta(days=6) == DEDUP_RESEND_MARGIN


def test_the_executor_reexports_the_awx_common_margin():
    from awx_spine import executor

    assert executor.DEDUP_RESEND_MARGIN is DEDUP_RESEND_MARGIN
