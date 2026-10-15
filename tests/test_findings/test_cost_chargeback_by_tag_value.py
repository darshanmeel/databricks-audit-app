"""tests/test_findings/test_cost_chargeback_by_tag_value.py.

Proves, against tests/fixtures/chargeback_b.py's own rows (tag keys 'cb2_team' and 'cb2_cc', see
that module's docstring for the full scenario table), that findings.f_cost_chargeback_by_tag_value
(grain [workspace_id, tag_key, tag_value], windowed on :period_days) has: the right grain, every
real tag key found in a workspace getting its own top-N + (other) + (untagged) set there, the
:top_values_per_key=10 rollup actually triggering on a 60-value key, the (untagged) bucket picking
up BOTH the other key's tagged rows and the fully-untagged rows (not just the latter), and every
status band (WARN, CRITICAL via percent AND via brand-new spend, OK via the $ floor even against a
huge swing).

chargeback_b.py's own scenario rows all carry workspace_id = NULL (DEC-15 account-level usage, see
that module's own docstring on why it cannot claim a dedicated workspace), so every row this file
checks is that one NULL-workspace bucket. The (untagged) rows below are checked against
dbutil.priced_usage_total(..., only_null_workspace=True)'s own independent recomputation of that
SAME bucket's total (never a hand-derived number -- the same reasoning tests/dbutil.py's own
usage_sum() docstring gives, "never hard-code a total"), since the (untagged) subtraction is done
per workspace now (see the query's own caveats). share_of_total_pct stays account-wide (unchanged
by the workspace split), so it is still checked against the plain, unfiltered
priced_usage_total().

Every OTHER expected number below (the top values and the (other) rollup, grouped by tag_key alone)
is computed by hand from tests/fixtures/chargeback_b.py's own scenario table, at the w=7 primary
window (current=[D7..D1], previous=[D14..D8]).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "fixtures"))
import dbutil  # noqa: E402

GRAINS_PATH = ROOT / "config" / "grains" / "cost_chargeback_by_tag_value.yml"
QID = "cost_chargeback_by_tag_value"
W = 7
MY_KEYS = {"cb2_team", "cb2_cc"}


def _grains():
    with open(GRAINS_PATH, "r", encoding="ascii") as f:
        return yaml.safe_load(f)


def _my_rows(window_days: int = W) -> list[dict]:
    return [r for r in dbutil.rows(QID, window_days) if r["tag_key"] in MY_KEYS]


def _by_key_value(rows_: list[dict]) -> dict:
    return {(r["tag_key"], r["tag_value"]): r for r in rows_}


def test_grain_uniqueness_and_window():
    grains = _grains()
    cols = grains[QID]
    out = _my_rows()
    assert len(out) == 17  # 5 (cb2_team: 4 values + untagged) + 12 (cb2_cc: 10 top + other + untagged)
    keys = [tuple(r[c] for c in cols) for r in out]
    assert len(keys) == len(set(keys)), "duplicate grain rows"
    for r in out:
        assert r["window_days"] == W
        assert r["status"] in {"OK", "WARN", "CRITICAL", "NOT_ASSESSED"}
        if not r["is_untagged"]:
            # is_other/individual-value rows are this fixture's own, fully priced SKUs; the
            # (untagged) row is now account-wide (Must #2 dropped the old per-usage_unit scoping),
            # so it also reflects whatever other builders' own unpriced scenarios exist and is not
            # this file's to assert on.
            assert r["price_basis"] == "priced"


def test_cb2_team_group_has_no_other_row():
    """Only 4 real 'cb2_team' values exist -- all fit under :top_values_per_key=10, so no
    (other) row is produced for this key."""
    by = _by_key_value(_my_rows())
    assert ("cb2_team", "(other)") not in by


def test_team_a_warn():
    r = _by_key_value(_my_rows())[("cb2_team", "cb2_team_a")]
    assert r["usd_list"] == 130.0
    assert r["prev_usd_list"] == 100.0
    assert r["change_pct"] == 30.0
    assert r["status"] == "WARN"
    assert r["is_other"] is False
    assert r["is_untagged"] is False


def test_team_b_critical():
    r = _by_key_value(_my_rows())[("cb2_team", "cb2_team_b")]
    assert r["usd_list"] == 200.0
    assert r["change_pct"] == 100.0
    assert r["status"] == "CRITICAL"


def test_team_c_ok_via_floor_despite_large_swing():
    r = _by_key_value(_my_rows())[("cb2_team", "cb2_team_c")]
    assert r["usd_list"] == 15.0
    assert r["prev_usd_list"] == 5.0
    assert r["change_pct"] == 200.0  # a real 200% swing, but...
    assert r["status"] == "OK"       # ...the floor (current < :min_spend_usd) overrides it


def test_team_d_critical_brand_new():
    r = _by_key_value(_my_rows())[("cb2_team", "cb2_team_d")]
    assert r["usd_list"] == 40.0
    assert r["prev_usd_list"] == 0.0
    assert r["change_pct"] is None
    assert r["status"] == "CRITICAL"
    assert r["not_assessed_reason"] is None


def test_cb2_cc_top_value_and_other_rollup():
    by = _by_key_value(_my_rows())
    # cc_000: rank 1 (amount 60), its own dedicated previous=20 -> +200% -> CRITICAL
    top = by[("cb2_cc", "cb2_cc_000")]
    assert top["usd_list"] == 60.0
    assert top["prev_usd_list"] == 20.0
    assert top["change_pct"] == 200.0
    assert top["status"] == "CRITICAL"
    assert top["is_other"] is False

    # cc_009: rank 10 (amount 51), last value still inside the top-10 cutoff -- 0% change, well
    # above the $ floor -> OK.
    last_top = by[("cb2_cc", "cb2_cc_009")]
    assert last_top["usd_list"] == 51.0
    assert last_top["status"] == "OK"
    assert last_top["is_other"] is False

    # cc_010 is rank 11 -- must NOT survive as its own row; it is rolled into (other) instead.
    assert ("cb2_cc", "cb2_cc_010") not in by

    other = by[("cb2_cc", "(other)")]
    assert other["usd_list"] == 1275.0     # sum of ranks 11-60: amounts 50..1 = sum(1..50)
    assert other["prev_usd_list"] == 1275.0
    assert other["change_pct"] == 0.0
    assert other["status"] == "OK"
    assert other["is_other"] is True


def test_untagged_rows_match_independent_account_total():
    """(untagged) for each key = this workspace's (here, the NULL/account-level bucket every row of
    this fixture lands in) whole total (recomputed independently via
    dbutil.priced_usage_total(only_null_workspace=True), never hard-coded) minus that key's own
    present total -- and it picks up BOTH the other key's tagged rows AND the fully-untagged rows,
    not just the latter."""
    cur_total = dbutil.priced_usage_total(W, current=True, only_null_workspace=True)
    prev_total = dbutil.priced_usage_total(W, current=False, only_null_workspace=True)
    by = _by_key_value(_my_rows())

    team_present_cur, team_present_prev = 385.0, 205.0   # 130+200+15+40, 100+100+5+0
    cc_present_cur, cc_present_prev = 1830.0, 1790.0      # sum(1..60), sum(1..60)-40

    team_untagged = by[("cb2_team", "(untagged)")]
    assert team_untagged["is_untagged"] is True
    assert team_untagged["usd_list"] == round(cur_total - team_present_cur, 2)
    assert team_untagged["prev_usd_list"] == round(prev_total - team_present_prev, 2)

    cc_untagged = by[("cb2_cc", "(untagged)")]
    assert cc_untagged["is_untagged"] is True
    assert cc_untagged["usd_list"] == round(cur_total - cc_present_cur, 2)
    assert cc_untagged["prev_usd_list"] == round(prev_total - cc_present_prev, 2)


def test_share_of_total_pct_is_account_wide():
    cur_total = dbutil.priced_usage_total(W, current=True)
    by = _by_key_value(_my_rows())
    r = by[("cb2_cc", "cb2_cc_000")]
    assert r["share_of_total_pct"] == round(r["usd_list"] * 100.0 / cur_total, 1)
    for r in by.values():
        assert 0 <= r["share_of_total_pct"] <= 100
