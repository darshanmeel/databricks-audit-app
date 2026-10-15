"""tests/test_findings/test_cost_chargeback_reconcile.py.

findings.f_cost_chargeback_reconcile (grain [workspace_id, check_name], windowed on
:period_days) reads system.billing.usage with no id filter of its own, one row per workspace (or
the NULL/account-level bucket) per check. Only two checks have coverage_type 'full' and a real gap
verdict - by_service/by_sku/by_workspace were dropped (each re-sums the exact same priced rows this
file already sums, so their own gap is always algebraically 0, a check with no power to ever catch
anything):

  - price_join_fanout: proven to reach both WARN and CRITICAL (at different windows) via
    tests/fixtures/reconcile.py's own rec_SKU_FANOUT scenario (two overlapping list_prices rows),
    all on workspace_id = NULL (account-level, DEC-15) - see that module's docstring for why one
    small, fixed-dollar gap lands in different bands at different window lengths, and why by_user
    was deliberately left without a dedicated fixture.
  - by_user: has no dedicated fixture (see tests/fixtures/reconcile.py), so it is only checked here
    for the same "never exceed / always OK absent a real gap" shape every full check without one
    would have, on every workspace's own row.

'overlapping' (by_tag_value) never flags a verdict and its view_total is at least the billing total
(a multi-tagged row can only add extra counting, never remove any), and 'partial' checks
(by_warehouse/by_job/by_cluster) never flag a verdict and never exceed the billing total -- these
invariants hold on every workspace's own row regardless of which other builders' rows are present,
so this file needs no dedicated data of its own to check them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_chargeback_reconcile.yml"
QID = "cost_chargeback_reconcile"
WINDOWS = (7, 30, 90)

FULL_CHECKS = {"price_join_fanout", "by_user"}
OVERLAPPING_CHECKS = {"by_tag_value"}
PARTIAL_CHECKS = {"by_warehouse", "by_job", "by_cluster"}
ALL_CHECKS = FULL_CHECKS | OVERLAPPING_CHECKS | PARTIAL_CHECKS

GAP_TOLERANCE_USD = 0.02


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _by_check(rows_: list[dict]) -> dict:
    """check_name -> every one of its own rows (one per workspace, or the NULL/account-level
    bucket) -- never a single dict keyed by check_name alone, which would silently keep only the
    LAST workspace's row for that check."""
    by: dict = {}
    for r in rows_:
        by.setdefault(r["check_name"], []).append(r)
    return by


def _null_ws_row(rows_: list[dict], check_name: str) -> dict:
    """The one row for `check_name` on workspace_id = NULL -- tests/fixtures/reconcile.py's own
    rec_SKU_FANOUT scenario is account-level (DEC-15), so this is the row it actually lands in."""
    matches = [r for r in rows_ if r["check_name"] == check_name and r["workspace_id"] is None]
    assert len(matches) == 1, f"expected exactly 1 NULL-workspace row for {check_name}, got {len(matches)}"
    return matches[0]


def test_grain_and_check_set_at_every_window():
    grains = _grains()
    cols = grains[QID]
    for w in WINDOWS:
        out = dbutil.rows(QID, w)
        assert len(out) >= len(ALL_CHECKS)
        assert len(out) % len(ALL_CHECKS) == 0, "every workspace must carry the full check set"
        keys = [tuple(r[c] for c in cols) for r in out]
        assert len(keys) == len(set(keys)), "duplicate grain rows"
        assert {r["check_name"] for r in out} == ALL_CHECKS
        for r in out:
            assert r["window_days"] == w
            assert r["status"] in {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}


def test_coverage_type_per_check():
    by = _by_check(dbutil.rows(QID, 30))
    for name in FULL_CHECKS:
        assert {r["coverage_type"] for r in by[name]} == {"full"}
    for name in OVERLAPPING_CHECKS:
        assert {r["coverage_type"] for r in by[name]} == {"overlapping"}
    for name in PARTIAL_CHECKS:
        assert {r["coverage_type"] for r in by[name]} == {"partial"}


def test_price_join_fanout_has_a_real_thousand_dollar_gap_everywhere():
    """tests/fixtures/reconcile.py's rec_SKU_FANOUT scenario overlaps two list_prices rows under
    one usage row - an exact, hand-computable $1,000 gap (1,000 quantity * $1.00) between the plain
    and de-duplicated join, present at every window regardless of what other builders contribute."""
    for w in WINDOWS:
        r = _null_ws_row(dbutil.rows(QID, w), "price_join_fanout")
        assert abs(r["gap_usd_list"] - 1000.0) <= GAP_TOLERANCE_USD
        assert r["status"] != "OK"


def test_price_join_fanout_reaches_both_warn_and_critical():
    """The same fixed $1,000 gap is a bigger share of a narrow window's smaller billing total than
    a wide window's larger one, so status varies by window - proving this check can read both
    bands, not only OK. Status is read off each row's own gap_pct against :gap_warn_pct/
    :gap_crit_pct rather than a hard-coded whole-suite total."""
    statuses = set()
    for w in WINDOWS:
        r = _null_ws_row(dbutil.rows(QID, w), "price_join_fanout")
        expected = "CRITICAL" if abs(r["gap_pct"]) >= 5 else "WARN" if abs(r["gap_pct"]) >= 1 else "OK"
        assert r["status"] == expected
        statuses.add(r["status"])
    assert "WARN" in statuses
    assert "CRITICAL" in statuses


def test_by_user_ok_with_no_dedicated_fixture():
    """No fixture here can add a dual-classified row without also disagreeing with
    tags.cost_reconciliation's own independent total (see tests/fixtures/reconcile.py's module
    docstring), so this check is only proven to stay OK and never exceed a real gap absent one, on
    every workspace's own row."""
    for w in WINDOWS:
        for r in _by_check(dbutil.rows(QID, w))["by_user"]:
            if r["status"] == "NOT_ASSESSED":
                continue  # a workspace with no priced spend at all has nothing to reconcile
            assert r["status"] == "OK"
            assert abs(r["gap_usd_list"]) <= GAP_TOLERANCE_USD


def test_overlapping_check_never_undercounts_and_is_always_ok():
    """by_tag_value explodes every key a row carries -- a multi-tagged row can only ADD extra
    counting, never remove any, so view_total can never fall short of the billing total, on every
    workspace's own row."""
    for w in WINDOWS:
        for r in _by_check(dbutil.rows(QID, w))["by_tag_value"]:
            assert r["status"] == "OK"
            if r["billing_total_usd_list"]:
                assert r["gap_usd_list"] >= -GAP_TOLERANCE_USD
                assert r["coverage_pct"] >= 100.0 - 0.1


def test_partial_checks_never_exceed_total_and_are_always_ok():
    for w in WINDOWS:
        by = _by_check(dbutil.rows(QID, w))
        for name in PARTIAL_CHECKS:
            for r in by[name]:
                assert r["status"] == "OK"
                if r["coverage_pct"] is not None:
                    assert 0.0 <= r["coverage_pct"] <= 100.0 + 0.1
