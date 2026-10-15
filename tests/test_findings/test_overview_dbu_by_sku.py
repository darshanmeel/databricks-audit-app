"""tests/test_findings/test_overview_dbu_by_sku.py -- T-10 port assertions.

The overview_dbu_by_sku model reads system.billing.usage rows from the billing fixture (T-09)
and aggregates net DBUs by workspace + SKU + product. Tests assert grain uniqueness, window
exclusions, and correctness of the aggregated totals against the raw fixture parquet.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Add parent directory to path to import dbutil
import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from dbutil import rows, usage_sum


def test_overview_dbu_by_sku():
    """Assert grain, window exclusions, and aggregation correctness of f_overview_dbu_by_sku."""
    query_id = "overview_dbu_by_sku"

    # (a) rows(id, 30) has >= 2 rows all with window_days == 30
    rows_30 = rows(query_id, 30)
    assert len(rows_30) >= 2, f"Expected >= 2 rows at window_days=30, got {len(rows_30)}"
    for row in rows_30:
        assert row["window_days"] == 30, f"Expected window_days=30, got {row['window_days']}"

    # (b) no duplicate grain (window_days, workspace_id, sku_name, billing_origin_product)
    grain_keys = [(r["window_days"], r["workspace_id"], r["sku_name"], r["billing_origin_product"]) for r in rows_30]
    assert len(grain_keys) == len(set(grain_keys)), f"Duplicate grain entries found in rows_30"

    # (c) sum of net_dbus equals usage_sum filtered to 30-day window within 1e-6
    # The query excludes current_date(), so the window is [AS_OF - 30 days, AS_OF)
    # In DuckDB SQL, we use DATE '2026-09-21' as the anchor (the test target's current_date())
    expected_sum = usage_sum(
        "usage_unit = 'DBU' AND usage_date >= DATE '2026-09-21' - INTERVAL 30 DAY AND usage_date < DATE '2026-09-21'"
    )
    actual_sum = sum(r["net_dbus"] for r in rows_30)
    assert abs(actual_sum - expected_sum) < 1e-6, \
        f"30-day sum mismatch: expected {expected_sum}, got {actual_sum}, diff {abs(actual_sum - expected_sum)}"

    # (d) per-row net_dbus equals the same sum filtered to that workspace/SKU/product
    for row in rows_30:
        ws_id = row["workspace_id"]
        sku = row["sku_name"]
        product = row["billing_origin_product"]
        # Filter by the row's workspace, SKU, and product - NULL = NULL is not TRUE in SQL, so a
        # NULL workspace_id or billing_origin_product each need their own IS NULL clause.
        ws_clause = "workspace_id IS NULL" if ws_id is None else f"workspace_id = '{ws_id}'"
        product_clause = (
            "billing_origin_product IS NULL" if product is None else f"billing_origin_product = '{product}'"
        )
        where_clause = (
            f"usage_unit = 'DBU' AND usage_date >= DATE '2026-09-21' - INTERVAL 30 DAY AND usage_date < DATE '2026-09-21' "
            f"AND {ws_clause} AND sku_name = '{sku}' AND {product_clause}"
        )
        expected_row_sum = usage_sum(where_clause)
        actual_row_sum = row["net_dbus"]
        assert abs(actual_row_sum - expected_row_sum) < 1e-6, \
            f"Row sum mismatch for workspace_id={ws_id}, sku_name={sku}, billing_origin_product={product}: " \
            f"expected {expected_row_sum}, got {actual_row_sum}"

    # (e) rows(id, 90) sums more than 30 and rows(id, 7) sums less
    rows_90 = rows(query_id, 90)
    rows_7 = rows(query_id, 7)

    sum_30 = sum(r["net_dbus"] for r in rows_30)
    sum_90 = sum(r["net_dbus"] for r in rows_90)
    sum_7 = sum(r["net_dbus"] for r in rows_7)

    assert sum_90 > sum_30, f"Expected sum_90 > sum_30, got {sum_90} vs {sum_30}"
    assert sum_7 < sum_30, f"Expected sum_7 < sum_30, got {sum_7} vs {sum_30}"

    # (f) rows(id, 0) == []
    rows_0 = rows(query_id, 0)
    assert rows_0 == [], f"Expected no rows at window_days=0, got {len(rows_0)} rows"
