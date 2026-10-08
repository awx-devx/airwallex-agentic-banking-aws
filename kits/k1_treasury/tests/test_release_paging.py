"""The DUAL release cross-check's paged read of GET /transfers, including the live sandbox's
missing `has_more`."""

from __future__ import annotations

from datetime import timedelta

import awx_k1_treasury.release as release_mod
import pytest
from awx_spine.fake import Fault

SUPPLIER = {"obligation_ids": ["ob-supplier-eur"]}
EPOCH_NOW = __import__("awx_spine.fake", fromlist=["EPOCH"]).EPOCH
OFFICE = {"obligation_ids": ["ob-office-usd"]}


def test_dual_cross_check_reads_every_page(env):
    for _ in range(105):
        env.spend_elsewhere("USD", "1")
    rows = release_mod._all_pages(env.ctx().read, "/api/v1/transfers")
    assert len(rows) == 105


def test_dual_cross_check_blocks_when_pages_run_out(env, monkeypatch):
    monkeypatch.setattr(release_mod, "MAX_PAGES", 1)
    monkeypatch.setattr(release_mod, "PAGE_SIZE", 10)
    for _ in range(12):
        env.spend_elsewhere("USD", "1")
    env.fake.inject(
        "transfers.create",
        Fault.TIMEOUT_AFTER_SUBMIT,
        visible_after_s=timedelta(days=30).total_seconds(),
    )
    env.execute(OFFICE)
    env.advance(timedelta(days=8).total_seconds())
    rel = env.release("ob-office-usd", "never showed up after 8 days", approvers=["a1", "a2"])
    assert rel.state == "BLOCKED"
    assert any("cannot cross-check" in r for r in rel.proposal.reasons)


def test_has_more_rules_match_the_live_sandbox(monkeypatch):
    """Live 6 Oct: GET /transfers omits has_more. Without it the scan stops only on an empty
    page; a present non-bool has_more, or a page repeating seen ids, cannot be trusted."""
    monkeypatch.setattr(release_mod, "PAGE_SIZE", 2)
    pages = {"0": [{"id": "a"}, {"id": "b"}], "1": [{"id": "c"}]}
    calls = []

    def read(req):
        calls.append(req.query["page_num"])
        return {"items": pages.get(req.query["page_num"], [])}  # no has_more, as live

    assert [r["id"] for r in release_mod._all_pages(read, "/api/v1/transfers")] == ["a", "b", "c"]
    assert calls == ["0", "1", "2"]  # the short page 1 does not end it; empty page 2 does
    with pytest.raises(release_mod.TooManyPages):
        release_mod._all_pages(lambda r: {"items": [], "has_more": "false"}, "/api/v1/transfers")
    with pytest.raises(release_mod.TooManyPages):
        release_mod._all_pages(lambda r: {"has_more": False}, "/api/v1/transfers")


def test_a_server_capping_pages_below_page_size_is_read_to_the_end():
    """250 rows, the server caps every page at 50 and omits has_more: all 250 are read,
    because only an empty page ends the scan."""
    rows = [{"id": f"t{i}"} for i in range(250)]

    def read(req):
        n = int(req.query["page_num"])
        return {"items": rows[n * 50 : (n + 1) * 50]}

    assert len(release_mod._all_pages(read, "/api/v1/transfers")) == 250


def test_page_num_ignored_blocks_instead_of_looping():
    with pytest.raises(release_mod.TooManyPages, match="repeats"):
        release_mod._all_pages(lambda r: {"items": [{"id": "x"}]}, "/api/v1/transfers")


def test_the_cross_check_cap_is_exactly_max_pages_times_page_size(monkeypatch):
    """20 full pages (2,000 rows at the real sizes) are read to the
    end; one more row BLOCKS, and the message names the real cap."""
    monkeypatch.setattr(release_mod, "MAX_PAGES", 3)
    monkeypatch.setattr(release_mod, "PAGE_SIZE", 2)

    def reader(n):
        rows = [{"id": f"t{i}"} for i in range(n)]
        return lambda req: {"items": rows[int(req.query["page_num"]) * 2 :][:2]}

    assert len(release_mod._all_pages(reader(6), "/api/v1/transfers")) == 6
    assert len(release_mod._all_pages(reader(5), "/api/v1/transfers")) == 5
    with pytest.raises(release_mod.TooManyPages, match="more than 6 rows"):
        release_mod._all_pages(reader(7), "/api/v1/transfers")


def test_dual_release_reads_every_page_through_the_runner(env, monkeypatch):
    monkeypatch.setattr(release_mod, "PAGE_SIZE", 10)
    seen = []
    real = env.port.read

    def spy(req):
        if req.path == "/api/v1/transfers" and "page_num" in req.query:
            seen.append(dict(req.query))
        return real(req)

    env.port.read = spy
    env.fake.inject(
        "transfers.create",
        Fault.TIMEOUT_AFTER_SUBMIT,
        visible_after_s=timedelta(days=30).total_seconds(),
    )
    env.execute(OFFICE)
    for _ in range(25):
        env.spend_elsewhere("USD", "1")
    env.advance(timedelta(days=8).total_seconds())
    rel = env.release("ob-office-usd", "never showed up after 8 days", approvers=["a1", "a2"])
    assert rel.state == "SUCCEEDED", rel.detail
    assert {q["page_num"] for q in seen} >= {"0", "1", "2"}
    assert all("from_created_at" in q for q in seen)
