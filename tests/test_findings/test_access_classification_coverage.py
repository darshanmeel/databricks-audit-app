"""tests/test_findings/test_access_classification_coverage.py -- P3-CLASSIFY assertions.

The access_classification_coverage model is a windowless inventory query (domain
governance_access) that compares system.information_schema.tables against
system.data_classification.results, per catalog. The built information_schema__tables and
data_classification__results sources also carry governance.py's `gv_` rows and (for
information_schema__tables) ports.py's `pt_` rows, unioned by name into the same sources (DEC-15),
so every exact-value assertion below filters to this builder's own `cl_`-prefixed catalogs
(tests/fixtures/classify.py) rather than asserting over the whole table.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from dbutil import rows

WARN_COVERAGE_PCT = 80  # the query's own :warn_coverage_pct default


def test_access_classification_coverage():
    query_id = "access_classification_coverage"

    # (a) rows(id, 0) has >= 1 row (windowless inventory).
    rows_0 = rows(query_id, 0)
    assert len(rows_0) >= 1, f"Expected >= 1 rows at window_days=0 (windowless), got {len(rows_0)}"
    for row in rows_0:
        assert row["window_days"] == 0, f"Expected window_days=0, got {row['window_days']}"

    # (b) grain uniqueness: (window_days, catalog_name), over the whole result set (this
    # builder's rows plus governance.py's/ports.py's).
    grain_keys = [(r["window_days"], r["catalog_name"]) for r in rows_0]
    assert len(grain_keys) == len(set(grain_keys)), "Duplicate grain entries found"

    # (c) rows(id, 7/30/90) == [] (windowless query -- this id only ever populates window_days=0).
    for w in (7, 30, 90):
        rows_w = rows(query_id, w)
        assert rows_w == [], f"Expected no rows at window_days={w} (windowless query), got {len(rows_w)} rows"

    # (d) filter to this builder's own cl_-prefixed catalogs (DEC-15) -- the ids classify.py
    # itself wrote to information_schema.tables / data_classification.results.
    cl_rows = {r["catalog_name"]: r for r in rows_0 if r["catalog_name"].startswith("cl_")}
    assert set(cl_rows) == {"cl_cat_full", "cl_cat_partial", "cl_cat_zero"}, (
        f"Expected exactly cl_cat_full/cl_cat_partial/cl_cat_zero, got {sorted(cl_rows)}"
    )

    # (e) the 'system' catalog is excluded (table_catalog <> 'system') -- checked against the
    # whole result set, not the cl_-filtered dict, since classify.py and governance.py both
    # write table_catalog='system' rows and either alone would make this a real failure if the
    # exclusion were ever dropped.
    assert "system" not in {r["catalog_name"] for r in rows_0}, (
        "the 'system' catalog must be excluded (table_catalog <> 'system')"
    )

    # (f) cl_cat_full: 3 non-VIEW tables (cl_full_view and cl_infoschema_excluded excluded), all
    # 3 classified (cl_full_t1's second classification row, under a different class_tag/column,
    # must dedupe to one hit; cl_full_view's own classification row must not inflate this past 3).
    full = cl_rows["cl_cat_full"]
    assert full["tables_total"] == 3, f"cl_cat_full tables_total: expected 3, got {full['tables_total']}"
    assert full["tables_classified"] == 3, (
        f"cl_cat_full tables_classified: expected 3 (deduped, VIEW excluded), got {full['tables_classified']}"
    )
    assert full["covered_pct"] == 100.0, f"cl_cat_full covered_pct: expected 100.0, got {full['covered_pct']}"
    assert full["status"] == "OK", f"cl_cat_full status: expected OK (100 >= {WARN_COVERAGE_PCT}), got {full['status']}"
    assert full["not_assessed_reason"] is None

    # (g) cl_cat_partial: 4 non-VIEW tables, 1 classified -> 25% -> WARN.
    partial = cl_rows["cl_cat_partial"]
    assert partial["tables_total"] == 4
    assert partial["tables_classified"] == 1
    assert partial["covered_pct"] == 25.0
    assert partial["status"] == "WARN", (
        f"cl_cat_partial status: expected WARN (25 < {WARN_COVERAGE_PCT}), got {partial['status']}"
    )
    assert partial["not_assessed_reason"] is None

    # (h) cl_cat_zero: 2 non-VIEW tables, 0 classified -> a genuine, honest 0.0 -> WARN, never
    # NOT_ASSESSED (governance.py's own gv_ rows already give data_classification.results at
    # least one row account-wide, so the global "table is empty everywhere" branch cannot fire
    # on this fixture).
    zero = cl_rows["cl_cat_zero"]
    assert zero["tables_total"] == 2
    assert zero["tables_classified"] == 0
    assert zero["covered_pct"] == 0.0, (
        f"cl_cat_zero covered_pct: expected a real 0.0 (not NULL/NOT_ASSESSED), got {zero['covered_pct']}"
    )
    assert zero["status"] == "WARN"
    assert zero["not_assessed_reason"] is None

    # (i) ORDER BY worst-first (status, then covered_pct ascending): within the cl_ subset,
    # zero (WARN, 0.0) and partial (WARN, 25.0) both come before full (OK, 100.0), and between
    # the two WARN rows zero (the lower covered_pct) comes first -- checked directly on the rows
    # dbutil.rows() returns (no re-sort), by position within the full result list.
    cl_positions = {r["catalog_name"]: i for i, r in enumerate(rows_0) if r["catalog_name"].startswith("cl_")}
    assert cl_positions["cl_cat_zero"] < cl_positions["cl_cat_partial"] < cl_positions["cl_cat_full"], (
        f"Expected cl_cat_zero, then cl_cat_partial, then cl_cat_full in that order, got positions {cl_positions}"
    )
