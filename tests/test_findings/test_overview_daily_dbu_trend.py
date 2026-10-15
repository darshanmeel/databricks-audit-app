"""tests/test_findings/test_overview_daily_dbu_trend.py -- T-11 port assertions.

The overview_daily_dbu_trend model reads system.billing.usage rows from the billing fixture
(T-09) and aggregates net DBUs by workspace + date. Tests assert grain uniqueness, window
exclusions, and correctness of both the grand total and every per-row net_dbus value against the
raw fixture parquet.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Add parent directory to path to import dbutil
import sys
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from dbutil import rows, usage_sum

_WINDOW_30 = "usage_date >= DATE '2026-09-21' - INTERVAL 30 DAY AND usage_date < DATE '2026-09-21'"


def test_overview_daily_dbu_trend():
    """Assert grain, window exclusions, and correctness of f_overview_daily_dbu_trend."""
    query_id = "overview_daily_dbu_trend"

    # (a) rows(id, 30) has >= 1 row all with window_days == 30
    rows_30 = rows(query_id, 30)
    assert len(rows_30) >= 1, f"Expected >= 1 rows at window_days=30, got {len(rows_30)}"
    for row in rows_30:
        assert row["window_days"] == 30, f"Expected window_days=30, got {row['window_days']}"

    # (b) no duplicate grain (window_days, workspace_id, usage_date)
    grain_keys = [(r["window_days"], r["workspace_id"], r["usage_date"]) for r in rows_30]
    assert len(grain_keys) == len(set(grain_keys)), f"Duplicate grain entries found in rows_30"

    # (c) the grand total across all rows equals an independent usage_sum() derivation off the
    # raw fixture parquet, filtered to the same 30-day window and DBU unit as the model.
    sum_30 = sum(r["net_dbus"] for r in rows_30)
    expected_sum_30 = usage_sum(f"usage_unit = 'DBU' AND {_WINDOW_30}")
    assert abs(sum_30 - expected_sum_30) < 1e-6, (
        f"30-day total mismatch: expected {expected_sum_30}, got {sum_30}"
    )

    # (d) every per-row net_dbus equals the same sum filtered to that row's workspace_id and
    # usage_date (NULL workspace_id handled with IS NULL, per fixture SEC H's account-level row).
    for row in rows_30:
        ws_id = row["workspace_id"]
        usage_date = row["usage_date"]
        ws_clause = "workspace_id IS NULL" if ws_id is None else f"workspace_id = '{ws_id}'"
        where_clause = (
            f"usage_unit = 'DBU' AND {_WINDOW_30} AND {ws_clause} "
            f"AND usage_date = DATE '{usage_date}'"
        )
        expected_row_sum = usage_sum(where_clause)
        assert abs(row["net_dbus"] - expected_row_sum) < 1e-6, (
            f"Row sum mismatch for workspace_id={ws_id}, usage_date={usage_date}: "
            f"expected {expected_row_sum}, got {row['net_dbus']}"
        )

    # (e) window exclusions: rows(id, 90) sums more than 30 and rows(id, 7) sums less
    rows_90 = rows(query_id, 90)
    rows_7 = rows(query_id, 7)

    sum_90 = sum(r.get("net_dbus", 0) for r in rows_90)
    sum_7 = sum(r.get("net_dbus", 0) for r in rows_7)

    assert sum_90 > sum_30, f"Expected sum_90 > sum_30, got {sum_90} vs {sum_30}"
    assert sum_7 < sum_30, f"Expected sum_7 < sum_30, got {sum_7} vs {sum_30}"

    # (f) rows(id, 0) == []
    rows_0 = rows(query_id, 0)
    assert rows_0 == [], f"Expected no rows at window_days=0, got {len(rows_0)} rows"
