"""tests/test_findings/test_cost_chargeback_by_allocation_tag.py -- P3-CHARGEBACK.

f_cost_chargeback_by_allocation_tag reads system.billing.usage + system.billing.list_prices
(tests/fixtures/chargeback.py, the one builder for this query) and dollarizes chargeback by the
value of the cost_center/team allocation-tag keys (DEC-60 rule 4 normalization), with a distinct
row for spend that carries NEITHER key at all (is_missing_allocation_key = true) even when the
resource carries other, unrelated tags.

Review round: the builder now keeps everything under one workspace (cb.CB_WS), isolating each
scenario by usage_unit instead of by workspace (the query's own grain groups workspace_id +
usage_unit, so a distinct usage_unit per scenario gives the same isolation). This file's assertions
were updated to match, and two cases were added/changed for the review's fix to the free-vs-unpriced
missing-row logic: U_PRICE's missing bucket (entirely free-usage) now reads OK / share 0.0 / no
not_assessed_reason (a real $0, not a gap), and a new U_PRICE_UNPRICED group proves the
still-NOT_ASSESSED case where the missing bucket's own spend is a genuine pricing-coverage gap.

Every expected number below is hand-derived directly from tests/fixtures/chargeback.py's own
scenario map (reproduced in that file's module docstring) rather than re-running the model's own
SQL, and was cross-checked against an independent DuckDB rendering of the generated model
(dbt/models/findings/cost/f_cost_chargeback_by_allocation_tag.sql's own duckdb branch) before this
file was written -- every row below matched exactly.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from dbutil import rows  # noqa: E402
import chargeback as cb  # noqa: E402

QID = "cost_chargeback_by_allocation_tag"

ALL_WS = [cb.CB_WS]


def _by_key(rows_: list[dict]) -> dict:
    """{(workspace_id, usage_unit, allocation_key, allocation_value): row}."""
    return {
        (r["workspace_id"], r["usage_unit"], r["allocation_key"], r["allocation_value"]): r
        for r in rows_
    }


def test_grain_is_unique_and_window_days_is_set():
    rows_30 = rows(QID, 30, workspace_ids=ALL_WS)
    assert len(rows_30) >= 1
    for row in rows_30:
        assert row["window_days"] == 30
    keys = [
        (r["workspace_id"], r["usage_unit"], r["allocation_key"], r["allocation_value"])
        for r in rows_30
    ]
    assert len(keys) == len(set(keys)), "duplicate (workspace_id, usage_unit, allocation_key, allocation_value) grain"


def test_ok_band_low_missing_share():
    """U_OK: matched $80 (cost_center=cb_finance) + missing $10 (env=prod only, not the
    allocation key) = $90 total, 10/90 = 11.1% missing -> OK."""
    by = _by_key(rows(QID, 30, workspace_ids=ALL_WS))
    matched = by[(cb.CB_WS, cb.U_OK, "cost_center", "cb_finance")]
    missing = by[(cb.CB_WS, cb.U_OK, None, None)]

    assert matched["is_missing_allocation_key"] is False
    assert matched["net_usage_quantity"] == 80.0
    assert matched["net_list_cost_usd"] == 80.0
    assert matched["price_basis"] == "priced"
    assert matched["status"] == "OK"

    assert missing["is_missing_allocation_key"] is True
    assert missing["net_usage_quantity"] == 10.0
    assert missing["net_list_cost_usd"] == 10.0
    assert missing["share_of_unit_pct"] == 11.1
    assert missing["status"] == "OK"
    assert missing["not_assessed_reason"] is None


def test_warn_band_and_no_tags_at_all_counts_as_missing():
    """U_WARN: matched $70 (team=cb_growth) + missing $30 (NO tags at all -- {}) = $100,
    30% missing -> WARN. Proves the missing bucket also catches the classic "no tags at all" case,
    not only "other tags but not the allocation key"."""
    by = _by_key(rows(QID, 30, workspace_ids=ALL_WS))
    matched = by[(cb.CB_WS, cb.U_WARN, "team", "cb_growth")]
    missing = by[(cb.CB_WS, cb.U_WARN, None, None)]

    assert matched["net_list_cost_usd"] == 70.0
    assert matched["status"] == "OK"

    assert missing["net_list_cost_usd"] == 30.0
    assert missing["share_of_unit_pct"] == 30.0
    assert missing["status"] == "WARN"


def test_critical_band_at_exact_threshold_and_alt_spelling():
    """U_CRIT: matched $50 (costcenter, no separator -- normalizes to cost_center) + missing
    $50 (owner=cb_alice, an unrelated tag, NOT cost_center/team) = $100, exactly 50% missing ->
    CRITICAL (the default :crit_missing_pct=50 boundary is inclusive)."""
    by = _by_key(rows(QID, 30, workspace_ids=ALL_WS))
    matched = by[(cb.CB_WS, cb.U_CRIT, "cost_center", "cb_ops")]
    missing = by[(cb.CB_WS, cb.U_CRIT, None, None)]

    assert matched["net_list_cost_usd"] == 50.0
    assert matched["status"] == "OK"

    assert missing["net_list_cost_usd"] == 50.0
    assert missing["share_of_unit_pct"] == 50.0
    assert missing["status"] == "CRITICAL"


def test_cost_center_outranks_team_and_usage_unit_is_never_blended():
    """DBU: a row tagged BOTH cost_center=cb_finance and team=cb_other must be charged back under
    cost_center (never team); a team-only row keeps its own group; the DBU group has NO missing-key
    row at all (every DBU row here matches). GB is a separate usage_unit in the SAME workspace with
    its own independent missing-share verdict (CRITICAL at 50%), proving usage_unit is never
    blended into the DBU group's total."""
    all_rows = rows(QID, 30, workspace_ids=ALL_WS)
    by = _by_key(all_rows)

    both = by[(cb.CB_WS, "DBU", "cost_center", "cb_finance")]
    assert both["net_usage_quantity"] == 40.0
    assert not any(
        r["workspace_id"] == cb.CB_WS and r["usage_unit"] == "DBU"
        and r["allocation_key"] == "team" and r["allocation_value"] == "cb_other"
        for r in all_rows
    ), "the cost_center+team row must never also appear as a team='cb_other' group"

    team_only = by[(cb.CB_WS, "DBU", "team", "cb_solo")]
    assert team_only["net_usage_quantity"] == 10.0

    dbu_rows = [r for r in all_rows if r["workspace_id"] == cb.CB_WS and r["usage_unit"] == "DBU"]
    assert not any(r["is_missing_allocation_key"] for r in dbu_rows), (
        "no row in this workspace's DBU group is missing the allocation key -- the query must "
        "not emit a spurious is_missing_allocation_key row when none exists"
    )

    gb_matched = by[(cb.CB_WS, "GB", "cost_center", "cb_ops")]
    gb_missing = by[(cb.CB_WS, "GB", None, None)]
    assert gb_matched["net_list_cost_usd"] == 10.0   # 5 GB * $2.00
    assert gb_missing["net_list_cost_usd"] == 10.0   # 5 GB * $2.00
    assert gb_missing["share_of_unit_pct"] == 50.0
    assert gb_missing["status"] == "CRITICAL"


def test_price_basis_and_free_missing_spend_is_a_real_zero_not_not_assessed():
    """U_PRICE: a priced group (price_basis='priced'), an unpriced group with an allocation key
    (a real SKU with no matching list_prices row -- price_basis='unpriced', net_list_cost_usd
    NULL, never forced to 0, but its own status is still OK because it HAS an allocation key), and
    the missing-key group itself carrying only a FREE_USAGE SKU with no price row
    (price_basis='free', net_list_cost_usd NULL). Per the review fix, a missing-key bucket whose
    entire spend is free-usage is a real $0, not a coverage gap: it reads status OK with
    share_of_unit_pct 0.0 and no not_assessed_reason -- never NOT_ASSESSED (see
    test_unpriced_missing_spend_reads_not_assessed below for the genuine-gap case)."""
    by = _by_key(rows(QID, 30, workspace_ids=ALL_WS))

    priced = by[(cb.CB_WS, cb.U_PRICE, "cost_center", "cb_finance")]
    assert priced["net_list_cost_usd"] == 10.0
    assert priced["price_basis"] == "priced"
    assert priced["status"] == "OK"

    unpriced = by[(cb.CB_WS, cb.U_PRICE, "team", "cb_ghost")]
    assert unpriced["net_list_cost_usd"] is None
    assert unpriced["price_basis"] == "unpriced"
    assert unpriced["status"] == "OK"  # not the missing-key row -- its own status is never judged

    missing = by[(cb.CB_WS, cb.U_PRICE, None, None)]
    assert missing["net_usage_quantity"] == 8.0
    assert missing["net_list_cost_usd"] is None
    assert missing["price_basis"] == "free"
    assert missing["status"] == "OK"
    assert missing["share_of_unit_pct"] == 0.0
    assert missing["not_assessed_reason"] is None


def test_unpriced_missing_spend_reads_not_assessed():
    """U_PRICE_UNPRICED: a priced allocated group (cost_center=cb_finance, $20) plus a missing-key
    group whose only spend is a genuinely unpriced (non-free) SKU (cb_MYSTERY_SKU, no list_prices
    row) -- a real pricing-coverage gap, unlike U_PRICE's free-only missing bucket above. Because
    the missing group's OWN dollars are NULL from a real gap, its share of spend cannot be judged
    honestly, so it must read NOT_ASSESSED -- never a silently-defaulted OK, and never a NULL
    comparison miscomputed as WARN/CRITICAL (DEC-57/58: NOT_ASSESSED is never OK, but a real
    NOT_ASSESSED must not be skipped either)."""
    by = _by_key(rows(QID, 30, workspace_ids=ALL_WS))

    priced = by[(cb.CB_WS, cb.U_PRICE_UNPRICED, "cost_center", "cb_finance")]
    assert priced["net_list_cost_usd"] == 20.0
    assert priced["price_basis"] == "priced"
    assert priced["status"] == "OK"

    missing = by[(cb.CB_WS, cb.U_PRICE_UNPRICED, None, None)]
    assert missing["net_usage_quantity"] == 6.0
    assert missing["net_list_cost_usd"] is None
    assert missing["price_basis"] == "unpriced"
    assert missing["status"] == "NOT_ASSESSED"
    assert missing["not_assessed_reason"] == "missing_allocation_spend_unpriced"


def test_null_workspace_reaches_the_unit_totals_join():
    """U_NULL_WS: account-level spend (workspace_id NULL, $15, fully allocated to cost_center=
    cb_finance) used to vanish entirely -- an INNER JOIN on a plain `=` never matches NULL to NULL,
    so this row's own group could never find its unit_totals match. It must now appear, priced,
    with share_of_unit_pct 100.0 (fully matched, none missing). dbutil.rows() cannot filter to
    workspace_id IS NULL (an IN-list never matches NULL), so this reads every row and filters in
    Python -- same convention tests/fixtures/finops.py's own account-level rows use."""
    row = _by_key([r for r in rows(QID, 30) if r["workspace_id"] is None])[
        (None, cb.U_NULL_WS, "cost_center", "cb_finance")
    ]
    assert row["is_missing_allocation_key"] is False
    assert row["net_list_cost_usd"] == 15.0
    assert row["price_basis"] == "priced"
    assert row["status"] == "OK"
    assert row["share_of_unit_pct"] == 100.0


def test_window_boundaries_and_as_of_day_excluded():
    """U_WINDOW: one (cost_center, cb_win) group at D(5)=5, D(20)=7, D(45)=11, D0(today)=999
    (always excluded). Expected sums: 5 @ w=7, 12 @ w=30, 23 @ w=90."""
    key = (cb.CB_WS, cb.U_WINDOW, "cost_center", "cb_win")

    row_7 = _by_key(rows(QID, 7, workspace_ids=ALL_WS))[key]
    row_30 = _by_key(rows(QID, 30, workspace_ids=ALL_WS))[key]
    row_90 = _by_key(rows(QID, 90, workspace_ids=ALL_WS))[key]

    assert row_7["net_usage_quantity"] == 5.0
    assert row_30["net_usage_quantity"] == 12.0
    assert row_90["net_usage_quantity"] == 23.0

    for row in (row_7, row_30, row_90):
        assert row["net_list_cost_usd"] == row["net_usage_quantity"], "SKU_WINDOW is priced $1.00/unit"

    # the D0 (today) row's 999 must never appear at any window -- confirmed indirectly: if it
    # leaked in, net_usage_quantity at w=90 would be 1022.0, not 23.0.
    assert row_90["net_usage_quantity"] == 23.0


def test_allocation_keys_follow_thresholds():
    """allocation_keys in config/thresholds.yml picks the keys: with only team, the row tagged
    both cost_center and team charges back under team, and cost_center alone counts as missing."""
    import duckdb
    import jinja_stub

    model = (ROOT / "dbt" / "models" / "findings" / "cost" / f"f_{QID}.sql").read_text(encoding="utf-8")
    sql = jinja_stub.render(
        model, windows=(30,), thresholds={QID: {"allocation_keys": "team"}},
        source=jinja_stub.parquet_source((ROOT / "tests" / "fixtures" / "parquet").as_posix()),
    )
    con = duckdb.connect()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        out = [dict(zip(cols, r)) for r in cur.fetchall() if r[cols.index("workspace_id")] == cb.CB_WS]
    finally:
        con.close()
    assert {r["allocation_key"] for r in out} <= {"team", None}
    by = _by_key(out)
    assert by[(cb.CB_WS, "DBU", "team", "cb_other")]["net_usage_quantity"] == 40.0
    assert by[(cb.CB_WS, "DBU", "team", "cb_solo")]["net_usage_quantity"] == 10.0
    assert by[(cb.CB_WS, "GB", None, None)]["net_list_cost_usd"] == 20.0
