"""tests/test_findings/test_storage_target_table_discovery.py -- T-11 port assertions.

The storage_target_table_discovery model is a windowless inventory query that reads
system.information_schema.tables and discovers tables eligible for per-table analysis. The
built information_schema__tables source also carries 8 governance_access fixture rows
(tests/fixtures/governance.py's `gv_` ids, unioned by name into the same source), so every
assertion below filters to this builder's own `pt_`-prefixed rows (DEC-15) rather than asserting
over the whole table.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Add parent directory to path to import dbutil
import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from dbutil import rows


def test_storage_target_table_discovery():
    """Assert grain and window behavior of f_storage_target_table_discovery (windowless
    inventory), filtered to this builder's own pt_-prefixed rows."""
    query_id = "storage_target_table_discovery"

    # (a) rows(id, 0) has >= 1 row (windowless inventory)
    rows_0 = rows(query_id, 0)
    assert len(rows_0) >= 1, f"Expected >= 1 rows at window_days=0 (windowless), got {len(rows_0)}"

    for row in rows_0:
        assert row["window_days"] == 0, f"Expected window_days=0, got {row['window_days']}"

    # (b) no VIEWs anywhere in the result (WHERE table_type <> 'VIEW')
    for row in rows_0:
        assert row["table_type"] != "VIEW", f"Found VIEW in results, expected all non-VIEWs: {row}"

    # (c) grain uniqueness: (window_days, table_catalog, table_schema, table_name), over the
    # whole result set (this builder's rows plus governance.py's gv_ rows).
    grain_keys = [(r["window_days"], r["table_catalog"], r["table_schema"], r["table_name"]) for r in rows_0]
    assert len(grain_keys) == len(set(grain_keys)), f"Duplicate grain entries found"

    # (d) filter to this builder's own pt_-prefixed rows (DEC-15) -- the ids ports.py itself
    # wrote to system.information_schema.tables.
    pt_rows = [r for r in rows_0 if r["table_catalog"].startswith("pt_")]
    pt_names = {r["table_name"] for r in pt_rows}

    # pt_view_1 (table_type='VIEW') must be excluded by the query's WHERE clause.
    assert "pt_view_1" not in pt_names, f"pt_view_1 (a VIEW) leaked into the results: {pt_names}"

    # pt_table_1 / pt_table_2 / pt_table_3 (all non-VIEW) must be present.
    assert {"pt_table_1", "pt_table_2", "pt_table_3"}.issubset(pt_names), (
        f"Expected pt_table_1..3 present, got pt_ names {pt_names}"
    )

    # (e) target_table is the catalog.schema.name concatenation, for every pt_ row.
    for row in pt_rows:
        expected_target = f"{row['table_catalog']}.{row['table_schema']}.{row['table_name']}"
        assert row["target_table"] == expected_target, (
            f"target_table mismatch: expected {expected_target}, got {row['target_table']}"
        )

    # (f) ORDER BY table_catalog, table_schema, table_name -- checked directly on the rows
    # dbutil.rows() returns (no re-sort), over this builder's own pt_ rows.
    pt_keys = [(r["table_catalog"], r["table_schema"], r["table_name"]) for r in pt_rows]
    assert pt_keys == sorted(pt_keys), (
        f"pt_ rows are not in (table_catalog, table_schema, table_name) order: {pt_keys}"
    )

    # (g) rows(id, 7/30/90) == [] (windowless query -- this snapshot id only ever populates
    # window_days=0).
    for w in (7, 30, 90):
        rows_w = rows(query_id, w)
        assert rows_w == [], f"Expected no rows at window_days={w} (windowless query), got {len(rows_w)} rows"
